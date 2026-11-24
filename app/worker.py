import json
import logging
import subprocess
import threading
import time
from datetime import timedelta

from sqlalchemy import and_, or_, select, update

from pipeline import studio
from . import agents, providers
from .config import ROOT, settings
from .db import Asset, Job, Project, Reference, event, init_db, new_id, now, session
from .media import probe
from .schemas import EditPlan, validate_assets

logger = logging.getLogger("harness.worker")


def claim_job():
    with session() as db:
        stale = db.scalars(select(Job).where(
            Job.status == "running", Job.started_at < now() - timedelta(minutes=2))).all()
        for job in stale:
            job.status = "attention"
            job.error = "Worker прерван. Проверьте внешний сервис перед повтором операции"
        candidate = db.scalar(select(Job).where(or_(
            Job.status == "queued",
            and_(Job.status == "waiting", Job.started_at < now() - timedelta(seconds=15))
        )).order_by(Job.created_at).with_for_update(skip_locked=True).limit(1))
        if not candidate:
            return None
        # Compare-and-swap also protects SQLite, whose SELECT FOR UPDATE is ignored.
        changed = db.execute(update(Job).where(
            Job.id == candidate.id, Job.status == candidate.status).values(
                status="running", started_at=now(), attempts=Job.attempts + 1))
        return candidate.id if changed.rowcount else None


def heartbeat(job_id, stop):
    while not stop.wait(15):
        with session() as db:
            db.execute(update(Job).where(Job.id == job_id, Job.status == "running").values(started_at=now()))


def render(project, assets, job_id):
    plan = EditPlan.model_validate(project.edit)
    validate_assets(plan, {a.id: a for a in assets})
    props = plan.model_dump()
    props["assets"] = {a.id: settings.media_base_url.rstrip("/") + "/media/" + a.path for a in assets}
    props_path = settings.data_dir / "work" / f"{job_id}.json"
    output = settings.data_dir / "renders" / f"{project.id}-{job_id}.mp4"
    props_path.write_text(json.dumps(props, ensure_ascii=False))
    process = subprocess.run(
        ["node", "render.mjs", str(props_path), str(output)], cwd=ROOT / "renderer",
        capture_output=True, text=True, timeout=1800,
    )
    if process.returncode:
        raise ValueError("Ошибка рендера Remotion. " + process.stderr[-1600:])
    metadata = probe(output)
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    if video["width"] != 1080 or video["height"] != 1920:
        raise ValueError("Рендер не прошёл проверку разрешения 1080×1920")
    return str(output.relative_to(settings.data_dir))


def process_job(job_id):
    with session() as db:
        job = db.get(Job, job_id)
        project = db.get(Project, job.project_id) if job.project_id else None
        assets = db.scalars(select(Asset).where(Asset.project_id == job.project_id,
            Asset.kind.in_(["footage", "avatar", "insert"]))).all() if project else []
    result = {}
    next_job = None
    if job.kind == "script":
        reference_evidence = {}

        def persist_evidence(evidence):
            reference_evidence["reference_evidence"] = evidence
            with session() as db:
                db.get(Job, job.id).result = dict(reference_evidence)

        result = None
        reference_id = job.payload.get("reference_id")
        if reference_id:
            result = studio.prepare(project, reference_id, job.payload.get("feedback", ""), persist_evidence)
        if result is not None:
            reference_evidence["script_strategy"] = "concept_rewrite"
        else:
            result = agents.generate_script(project, job.payload.get("feedback", ""), job.payload.get("research", False), job.payload.get("reference_id"))
        with session() as db:
            stored = db.get(Project, project.id)
            if stored.script:
                event(db, project.id, "script_version", script=stored.script)
            stored.script, stored.status = result, "script_review"
        result = {**result, **reference_evidence}
    elif job.kind == "pipeline_video":
        raw, output = studio.produce(project,job.id)
        duration = float(probe(settings.data_dir/raw)["format"]["duration"])
        with session() as db:
            stored = db.get(Project,project.id)
            stored.render_path, stored.status = output, "video_review"
            if not db.get(Asset,job.id):
                db.add(Asset(id=job.id, project_id=project.id, name="HeyGen", kind="avatar",
                             path=raw,duration=duration))
        result = {"path":output}
    elif job.kind == "pipeline_edit":
        output = studio.revise_edit(project, job.id, assets, job.payload.get("feedback", ""))
        with session() as db:
            stored = db.get(Project, project.id)
            stored.render_path, stored.status = output, "video_review"
        result = {"path": output}
    elif job.kind == "analyze":
        with session() as db:
            asset = db.get(Asset, job.payload["asset_id"])
        transcript, analysis = agents.analyze_asset(asset)
        with session() as db:
            db.get(Asset, asset.id).transcript = transcript
            for reference in db.scalars(select(Reference).where(Reference.asset_id == asset.id)).all():
                reference.analysis = analysis
        result = {"analysis": analysis}
    elif job.kind in ("avatar", "insert"):
        remote_id = (job.result or {}).get("video_id")
        if not remote_id:
            response = (providers.create_avatar(project, job.id) if job.kind == "avatar"
                        else providers.create_insert(job.payload, job.id))
            remote_id = response.get("video_id") or response.get("id")
            if not remote_id:
                raise ValueError("HeyGen принял запрос, но не вернул video_id. Проверьте кабинет")
            with session() as db:
                db.get(Job, job.id).result = {"video_id": remote_id}
        path = "videos/" if job.kind == "avatar" else "models/videos/"
        response = providers.heygen_request("GET", path + remote_id)
        if response["status"] in ("failed", "cancelled"):
            raise ValueError("HeyGen завершил генерацию с ошибкой. Проверьте кабинет")
        if response["status"] != "completed":
            with session() as db:
                stored = db.get(Job, job.id)
                stored.status, stored.started_at = "waiting", now()
            return
        asset_id = new_id()
        relative = f"uploads/{asset_id}.mp4"
        target = settings.data_dir / relative
        providers.download_video(response["video_url"], target)
        duration = float(probe(target)["format"]["duration"])
        with session() as db:
            db.add(Asset(id=asset_id, project_id=project.id, name=f"HeyGen {job.kind}",
                         kind=job.kind, path=relative, duration=duration))
        result = {"video_id": remote_id, "asset_id": asset_id}
        if job.kind == "avatar" and job.payload.get("auto_render"):
            next_job = Job(project_id=project.id, kind="edit", payload={"auto_render": True})
    elif job.kind == "edit":
        if job.payload.get("auto_render"):
            next_job = Job(project_id=project.id, kind="render", payload={})
        result = agents.create_edit(project, assets, job.payload.get("feedback", ""))
        with session() as db:
            stored = db.get(Project, project.id)
            stored.edit, stored.status = result, "edit_review"
    elif job.kind == "render":
        output = render(project, assets, job.id)
        with session() as db:
            stored = db.get(Project, project.id)
            stored.render_path, stored.status = output, "video_review"
        result = {"path": output}
    elif job.kind == "schedule":
        response = providers.schedule_post(project, job.payload)
        data = response.get("data", response)
        post_id = data.get("id") if isinstance(data, dict) else None
        if not post_id:
            raise ValueError("Metricool принял запрос без ожидаемого id. Проверьте календарь перед повтором")
        result = {"post_id": str(post_id)}
        with session() as db:
            stored = db.get(Project, project.id)
            stored.post_id, stored.publication, stored.status = str(post_id), response, "scheduled"
    elif job.kind == "publication_status":
        result = providers.metricool_request("GET", "scheduler/posts/" + project.post_id)
        with session() as db:
            db.get(Project, project.id).publication = result
    else:
        raise ValueError("Неизвестный тип задачи")
    with session() as db:
        stored = db.get(Job, job.id)
        stored.status, stored.result = "completed", result
        if next_job is not None:
            db.add(next_job)
        event(db, job.project_id, "job_completed", job_id=job.id, job_kind=job.kind)


def execute(job_id):
    stop = threading.Event()
    thread = threading.Thread(target=heartbeat, args=(job_id, stop), daemon=True)
    thread.start()
    try:
        process_job(job_id)
    except Exception as exc:
        # External POSTs may have succeeded before a connection was interrupted.
        # Preserve that uncertainty, so the UI never offers a blind duplicate POST.
        with session() as db:
            job = db.get(Job, job_id)
            uncertain = job.kind in ("avatar", "insert", "schedule")
            job.status = "attention" if uncertain else "failed"
            message = str(exc)
            for secret in (settings.openai_api_key, settings.heygen_api_key, settings.metricool_token):
                if secret:
                    message = message.replace(secret, "[скрыто]")
            job.error = message[:2000]
            event(db, job.project_id, "job_failed", job_id=job.id, error=job.error)
        logger.warning("Job %s failed (%s)", job_id, type(exc).__name__)
    finally:
        stop.set()
        thread.join(timeout=2)


def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    while True:
        try:
            job_id = claim_job()
            if job_id:
                execute(job_id)
            else:
                time.sleep(2)
        except Exception:
            logger.exception("Ошибка очереди")
            time.sleep(5)


if __name__ == "__main__":
    main()
