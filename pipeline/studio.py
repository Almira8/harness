"""Bridge between review records in the studio and durable pipeline checkpoints."""
import shutil

from app.config import settings
from app.db import Asset, Job, Reference, session
from sqlalchemy import select
from . import analyzer, composer, extractor, scriptwriter, visuals
from .common import dump, read, workspace
from .originality import check_draft
from schemas.pipeline import ScriptPlan


def prepare(project, reference_id, feedback, on_evidence=lambda evidence: None):
    with session() as db:
        ref = db.get(Reference,reference_id)
        asset = db.get(Asset,ref.asset_id) if ref and ref.asset_id else None
        if not asset or not asset.path.lower().endswith((".mp4",".mov",".m4v",".webm")):
            return None
        path = settings.data_dir/asset.path
        notes = ref.notes
        recent = db.scalars(select(Job.result).where(
            Job.kind == "script", Job.status == "completed",
            Job.payload["reference_id"].as_string() == reference_id,
        ).order_by(Job.created_at.desc()).limit(5)).all()
        previous_narrations = [
            " ".join(scene.get("narration", "") for scene in script.get("scenes", []))
            for script in [project.script, *recent] if script
        ]
    with workspace(project.id) as directory:
        identity = directory/"job.json"
        meta = read(identity) if identity.exists() else {}
        if meta.get("ref") != str(path) or meta.get("reference_analysis_version") != 4:
            for name in ("1_extracted.json","2_analysis.json","3_script_plan.json"):
                (directory/name).unlink(missing_ok=True)
            dump(identity,{"ref":str(path),"bg":"studio_dark","aspect":"9:16","reference_analysis_version":4})
        if not (directory/"1_extracted.json").exists():
            extractor.extract(path,directory)
        extracted = read(directory/"1_extracted.json")
        evidence = {key: extracted[key] for key in ("transcript", "screen_text", "ocr_warning")}
        on_evidence(evidence)
        if analyzer.needs_analysis(directory) or meta.get("semantic_analysis_version") != analyzer.ANALYSIS_VERSION:
            analyzer.analyze(directory)
            meta = read(identity)
            dump(identity, {**meta, "semantic_analysis_version": analyzer.ANALYSIS_VERSION})
        analysis = read(directory/"2_analysis.json")
        on_evidence({**evidence, "topic": analysis["core_insight"],
                     "topic_supported": bool(analysis["topic_problem"] and analysis["core_insight"])})
        scriptwriter.write(directory, "studio_dark", project.brief + "\n" + notes, feedback,
                           needs_rewrite=check_draft(directory, previous_narrations))
        plan = ScriptPlan.model_validate(read(directory/"3_script_plan.json"))
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
            "seconds":seconds,"pipeline_job_id":project.id}


def produce(project,job_id):
    with workspace(project.id) as directory:
        raw = visuals.render(directory, studio_title=project.title)
        final = composer.compose(directory)
        raw_path = f"uploads/{job_id}-avatar.mp4"
        output = f"renders/{job_id}.mp4"
        shutil.copyfile(raw, settings.data_dir/raw_path)
        shutil.copyfile(final, settings.data_dir/output)
    return raw_path,output


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
    return output
