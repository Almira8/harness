"""Bridge between review records in the studio and durable pipeline checkpoints."""
import shutil

from app.config import settings
from app.db import Asset, Reference, session
from . import analyzer, composer, extractor, scriptwriter, visuals
from .common import dump, read, workspace
from schemas.pipeline import ScriptPlan


def prepare(project, reference_id, feedback):
    with session() as db:
        ref = db.get(Reference,reference_id)
        asset = db.get(Asset,ref.asset_id) if ref and ref.asset_id else None
        if not asset or not asset.path.lower().endswith((".mp4",".mov",".m4v",".webm")):
            return None
        path = settings.data_dir/asset.path
        notes = ref.notes
    with workspace(project.id) as directory:
        identity = directory/"job.json"
        meta = read(identity) if identity.exists() else {}
        if meta.get("ref") != str(path) or meta.get("reference_analysis_version") != 2:
            for name in ("1_extracted.json","2_analysis.json","3_script_plan.json"):
                (directory/name).unlink(missing_ok=True)
            dump(identity,{"ref":str(path),"bg":"studio_dark","aspect":"9:16","reference_analysis_version":2})
        if not (directory/"1_extracted.json").exists():
            extractor.extract(path,directory)
        if not (directory/"2_analysis.json").exists():
            analyzer.analyze(directory)
        scriptwriter.write(directory,"studio_dark",project.brief+"\n"+notes,feedback)
        plan = ScriptPlan.model_validate(read(directory/"3_script_plan.json"))
    scenes, seconds = [], []
    for scene in plan.scenes:
        words = scene.spoken_text.split()
        start = len(seconds)
        for i in range(0,len(words),2):
            seconds.append({"second":len(seconds),"speech":" ".join(words[i:i+2]),
                "action":scene.camera_framing+" · "+scene.background_prompt_or_asset,
                "animation":scene.overlay_text or scene.b_roll_query or "", "sound":"Голос"})
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
