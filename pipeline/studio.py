"""Bridge between review records in the studio and durable pipeline checkpoints."""
import shutil

from app.config import settings
from app.db import Asset, Job, Reference, session
from sqlalchemy import select
from . import analyzer, composer, extractor, scriptwriter, visuals
from .common import digest, dump, read, workspace
from schemas.pipeline import HookOption, ScriptPlan
from services import background_manager as backgrounds


REFERENCE_EXTRACTION_VERSION = 5


def copy_extraction(source, target):
    manifest = source / "1_extracted.json"
    extraction = read(manifest)
    files = {extraction.get(field) for field in ("audio", "preview", "contact_sheet")}
    files.update(sample["frame"] for sample in extraction.get("samples", []))
    files.update(cut["frame"] for cut in extraction.get("cuts", []))
    for name in files - {None, ""}:
        original = composer.source(source, name)
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, destination)
    dump(target / manifest.name, extraction)


def reference_checkpoints(path, directory, previous_projects, on_evidence):
    cache_key = backgrounds.fingerprint([
        digest(path), REFERENCE_EXTRACTION_VERSION, settings.transcription_model, settings.transcription_language,
    ])
    with workspace(f"reference-{cache_key}") as cache:
        extracted_path = cache / "1_extracted.json"
        checked_path = cache / "2_analysis.json"
        version_file = cache / "analysis_version.json"
        for old_directory in [directory, *previous_projects]:
            identity = old_directory / "job.json"
            if extracted_path.exists() and checked_path.exists():
                break
            if not identity.exists() or not (old_directory / "1_extracted.json").exists():
                continue
            previous = read(identity)
            if (previous.get("ref") != str(path)
                    or previous.get("reference_analysis_version") != REFERENCE_EXTRACTION_VERSION
                    or previous.get("reference_cache_key") not in {None, cache_key}):
                continue
            if not extracted_path.exists():
                copy_extraction(old_directory, cache)
            if (not checked_path.exists() and previous.get("semantic_analysis_version") == analyzer.ANALYSIS_VERSION
                    and not analyzer.needs_analysis(old_directory)):
                dump(checked_path, read(old_directory / checked_path.name))
                dump(version_file, {"version": analyzer.ANALYSIS_VERSION})
        if not extracted_path.exists():
            extractor.extract(path, cache)
        extracted = read(extracted_path)
        project_extraction = directory / extracted_path.name
        project_identity = directory / "job.json"
        current = read(project_identity) if project_identity.exists() else {}
        if not project_extraction.exists() or current.get("reference_cache_key") not in {None, cache_key}:
            copy_extraction(cache, directory)
        dump(project_identity, {**current, "reference_cache_key": cache_key})
        evidence = {key: extracted[key] for key in ("transcript", "screen_text", "ocr_warning")}
        on_evidence(evidence)
        if (analyzer.needs_analysis(cache) or not version_file.exists()
                or read(version_file).get("version") != analyzer.ANALYSIS_VERSION):
            analyzer.analyze(cache)
            dump(version_file, {"version": analyzer.ANALYSIS_VERSION})
        analysis = read(checked_path)
        dump(directory / checked_path.name, analysis)
        return cache_key, evidence, analysis


def prepare(project, reference_id, feedback, on_evidence=lambda evidence: None):
    with session() as db:
        ref = db.get(Reference,reference_id)
        asset = db.get(Asset,ref.asset_id) if ref and ref.asset_id else None
        if not asset or not asset.path.lower().endswith((".mp4",".mov",".m4v",".webm")):
            return None
        path = settings.data_dir/asset.path
        notes = ref.notes
        recent = db.execute(select(Job.project_id, Job.result).where(
            Job.kind == "script", Job.status == "completed",
            Job.payload["reference_id"].as_string() == reference_id,
        ).order_by(Job.created_at.desc()).limit(5)).all()
    with workspace(project.id) as directory:
        identity = directory/"job.json"
        meta = read(identity) if identity.exists() else {}
        if meta.get("ref") != str(path) or meta.get("reference_analysis_version") != REFERENCE_EXTRACTION_VERSION:
            for name in ("1_extracted.json", "2_analysis.json", "3_script_plan.json"):
                (directory / name).unlink(missing_ok=True)
            meta = {"ref": str(path), "bg": "studio_dark", "aspect": "9:16",
                    "reference_analysis_version": REFERENCE_EXTRACTION_VERSION}
            dump(identity, meta)
        previous_projects = [settings.pipeline_workspace / identifier for identifier, _ in recent
                             if identifier and identifier != project.id]
        cache_key, evidence, analysis = reference_checkpoints(path, directory, previous_projects, on_evidence)
        dump(identity, {**meta, "semantic_analysis_version": analyzer.ANALYSIS_VERSION,
                        "reference_cache_key": cache_key})
        on_evidence({**evidence, "topic": analysis["topic_and_tools"],
                     "topic_supported": bool(analysis["topic_and_tools"].strip())})
        scriptwriter.write(directory, "studio_dark", project.brief + "\n" + notes, feedback)
        plan = ScriptPlan.model_validate(read(directory/"3_script_plan.json"))
    return review_script(project.id, plan)


def prepare_topic(project, topic, notes, hook_style, feedback=""):
    with workspace(project.id) as directory:
        scriptwriter.generate_from_topic(topic, notes, hook_style, directory=directory, feedback=feedback)
        plan = ScriptPlan.model_validate(read(directory / "3_script_plan.json"))
    return review_script(project.id, plan)


def review_script(project_id, plan):
    scenes, seconds = [], []
    for scene in plan.scenes:
        start = len(seconds)
        framing = "Ведущая крупным планом" if scene.camera_framing == "closeup" else "Ведущая по пояс"
        beats = scene.screen_beats or []
        if beats and " ".join(" ".join(beat.speech for beat in beats).split()) == " ".join(scene.spoken_text.split()):
            for beat in beats:
                seconds.append({**beat.model_dump(), "second": len(seconds),
                                "sound": "Голос" if beat.speech.strip() else "Пауза в речи"})
        else:
            words = scene.spoken_text.split()
            for i in range(0, len(words), 2):
                seconds.append({
                    "second": len(seconds), "speech": " ".join(words[i:i + 2]),
                    "action": framing,
                    "screen_text": scene.overlay_text or "",
                    "animation": "Появляется белая надпись" if i == 0 and scene.overlay_text else "",
                    "sound": "Голос",
                })
        scenes.append({"start":start,"end":len(seconds),"narration":scene.spoken_text,
            "screen_text":scene.overlay_text or "", "graphic":scene.b_roll_query or "",
            "layout":"presenter","animation":"fade","filming":scene.camera_framing,"materials":[]})
    return {"title":plan.title,"caption":plan.caption,"duration":len(seconds),"scenes":scenes,
            "seconds":seconds,"pipeline_job_id":project_id,
            "cover_hook_text": plan.cover_hook_text, "cover_hook_duration_sec": plan.cover_hook_duration_sec,
            "alternative_hooks": [item.model_dump() for item in (plan.alternative_hooks or [])],
            "selected_hook": plan.selected_hook}


def available_backgrounds(db):
    uploads = db.scalars(select(Asset).where(Asset.kind == "background").order_by(Asset.name, Asset.id)).all()
    return backgrounds.studio_library(uploads)


def can_swap_background(project):
    if project.mode != "avatar" or not project.render_path or project.edit or project.post_id:
        return False
    directory = settings.pipeline_workspace / project.id
    path = directory / "4_render.json"
    return path.is_file() and bool(read(path).get("backgrounds"))


def production_plan(project):
    return ScriptPlan.model_validate({
        "title": project.script["title"], "caption": project.script.get("caption", ""),
        "music": {"asset": None, "volume": 0}, "subtitle_style": "highlight",
        "cover_hook_text": project.script.get("cover_hook_text"),
        "cover_hook_duration_sec": project.script.get("cover_hook_duration_sec", 2.5),
        "scenes": [
            {"scene_id": index, "spoken_text": scene["narration"],
             "camera_framing": "closeup" if scene.get("filming") == "closeup" else "half_body",
             "background_type": "color", "background_prompt_or_asset": "studio_dark",
             "overlay_text": scene.get("screen_text") or None, "b_roll_query": scene.get("graphic") or None}
            for index, scene in enumerate(project.script["scenes"], 1) if scene["narration"].strip()
        ],
    })


def approved_plan(project, directory):
    path = directory / "3_script_plan.json"
    saved = project.script or {}
    if not path.is_file() or not saved.get("pipeline_job_id"):
        return production_plan(project)
    plan = ScriptPlan.model_validate(read(path))
    for scene, snapshot in zip(plan.scenes, saved["scenes"], strict=True):
        narration = snapshot["narration"]
        if scene.spoken_text != narration:
            scriptwriter.retime_speech(scene, narration)
            scene.spoken_text = narration
    for field in ("cover_hook_text", "cover_hook_duration_sec", "selected_hook", "alternative_hooks"):
        if field in saved:
            plan = ScriptPlan.model_validate({**plan.model_dump(), field: saved[field]})
    return plan


def edit_for_review(project, cover, narrations, selected_hook):
    with workspace(project.id) as directory:
        plan = approved_plan(project, directory)
        if len(narrations) != len(plan.scenes):
            raise ValueError("Число реплик должно совпадать с числом сцен")
        options = plan.alternative_hooks or []
        if selected_hook is not None and selected_hook >= len(options):
            raise ValueError("Такого варианта хука нет")
        opening, _ = scriptwriter.split_opening(narrations[0])
        if scriptwriter.cover_repeats_opening(cover, opening):
            raise ValueError("Заголовок обложки повторяет первую реплику. Сформулируй его иначе.")
        for scene, narration in zip(plan.scenes, narrations, strict=True):
            scriptwriter.retime_speech(scene, narration)
            scene.spoken_text = narration
        plan.cover_hook_text = cover
        plan.selected_hook = selected_hook
        if selected_hook is not None:
            options[selected_hook] = HookOption(spoken_hook=opening[:180], cover_hook_text=cover)
        return review_script(project.id, plan)


def publish_cover(directory, output):
    cover = directory / "5_cover.jpg"
    if cover.exists():
        shutil.copyfile(cover, (settings.data_dir / output).with_suffix(".jpg"))


def produce(project, job_id, bg_mode=None, bg="default_studio", local_edit=False):
    with workspace(project.id) as directory:
        presets = None
        plan_path = directory / "3_script_plan.json"
        if bg_mode or (project.script or {}).get("pipeline_job_id"):
            plan = approved_plan(project, directory)
            if bg_mode:
                with session() as db:
                    presets = available_backgrounds(db)
            if bg_mode == "auto_scene":
                choices = directory / f"backgrounds-{job_id}.json"
                if choices.is_file():
                    plan = ScriptPlan.model_validate(read(choices))
                else:
                    scriptwriter.assign_backgrounds(plan, presets, bg)
                    dump(choices, plan.model_dump())
            dump(plan_path, plan.model_dump())
        composer.preflight(ScriptPlan.model_validate(read(plan_path)))
        raw = visuals.render(directory, studio_title=project.title, bg_mode=bg_mode, bg=bg, presets=presets)
        final = composer.compose(directory, local_edit=local_edit)
        raw_path = f"uploads/{job_id}-avatar.mp4"
        output = f"renders/{job_id}.mp4"
        shutil.copyfile(raw, settings.data_dir/raw_path)
        shutil.copyfile(final, settings.data_dir/output)
        publish_cover(directory, output)
    return raw_path,output


def change_background(project, job_id, preset):
    with workspace(project.id) as directory:
        with session() as db:
            spec = backgrounds.preset_spec(preset, available_backgrounds(db))
        plate = backgrounds.stage_for_remotion(spec, directory)
        final = composer.replace_background(directory, plate)
        output = f"renders/{job_id}.mp4"
        shutil.copyfile(final, settings.data_dir / output)
        publish_cover(directory, output)
    return output


def revise_edit(project, job_id, assets, feedback):
    with workspace(project.id) as directory:
        uploaded = directory / "broll" / "uploaded"
        uploaded.mkdir(parents=True, exist_ok=True)
        for asset in assets:
            if asset.kind == "insert":
                source = settings.data_dir / asset.path
                name = f"{asset.id}-{asset.name.replace('/', '_')[:80]}"
                target = uploaded / (name + source.suffix)
                if not target.exists():
                    temp = target.with_suffix(".part")
                    shutil.copyfile(source, temp)
                    temp.replace(target)
        final = composer.compose(directory, feedback=feedback)
        output = f"renders/{job_id}.mp4"
        shutil.copyfile(final, settings.data_dir / output)
        publish_cover(directory, output)
    return output
