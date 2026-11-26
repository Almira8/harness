from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pipeline import studio
from services.background_manager import MEDIA, preset_spec

from .config import ROOT, settings
from .db import Asset, Event, Job, Project, Reference, event, init_db, new_id, session
from .media import probe
from .providers import public_url
from .schemas import (BackgroundInput, CreateProject, EditInput, EditPlan, GenerateClip, GenerateInput, ProductionInput,
                      ReferenceInput, ScheduleInput, Script, ScriptReviewInput, TopicInput, validate_assets)

ACTIVE = ("queued", "running", "waiting", "attention")


@asynccontextmanager
async def lifespan(app):
    init_db()
    with session() as db:
        if not db.scalar(select(Reference).limit(1)):
            shortcodes = ["Df-h0_NB5rN", "Df4pDlWRDd4", "DfnuR2uIqMb", "Dd9o-QfP2Nu",
                          "DeXds6BE2RL", "DfnQKYzi8cr", "DfVWruUIso9"]
            for index, shortcode in enumerate(shortcodes):
                notes = ("Нравится монтаж и композиция: сидящий автор показывает красивые, необычные "
                         "сгенерированные примеры рядом с собой. Есть практическая польза. "
                         "Графика синхронизирована с объяснением. Предпочтение подтверждено автором."
                         if index == 0 else "Референс из подборки Альмиры. Уточнить, что именно нравится.")
                db.add(Reference(url=f"https://www.instagram.com/p/{shortcode}/", notes=notes))
    yield


app = FastAPI(title="alylabb · Reels harness", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")
templates = Jinja2Templates(directory=ROOT / "app/templates")


@app.middleware("http")
async def same_origin(request: Request, call_next):
    # Local control panel uses same-origin requests. Reject browser cross-site writes.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail": "Откройте панель на том же адресе"}, status_code=403)
    return await call_next(request)


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=422)


@app.exception_handler(IntegrityError)
async def conflict(request, exc):
    return JSONResponse({"detail": "У выпуска уже есть активная задача"}, status_code=409)


def serialize(row):
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


def project_or_404(db, project_id):
    project = db.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if not project:
        raise HTTPException(404, "Выпуск не найден")
    return project


def require_idle(db, project_id):
    if db.scalar(select(Job).where(Job.project_id == project_id, Job.status.in_(ACTIVE)).limit(1)):
        raise HTTPException(409, "Сначала завершите или разберите текущую задачу выпуска")


def enqueue(db, project, kind, payload=None):
    if project:
        require_idle(db, project.id)
    job = Job(project_id=project.id if project else None, kind=kind, payload=payload or {})
    db.add(job)
    db.flush()
    return serialize(job)


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/api/status")
def status():
    return {"openai": bool(settings.openai_api_key),
            "heygen": bool(settings.heygen_api_key and settings.heygen_avatar_id and settings.heygen_voice_id),
            "video_generation": bool(settings.heygen_api_key),
            "metricool": bool(settings.metricool_token and settings.metricool_user_id),
            "model": settings.openai_model}


@app.get("/api/projects")
def projects():
    with session() as db:
        return [serialize(p) for p in db.scalars(select(Project).order_by(Project.created_at.desc())).all()]


@app.get("/api/queue")
def publication_queue():
    with session() as db:
        rows = db.execute(select(Project, Job).join(Job, Job.project_id == Project.id).where(
            Project.status == "scheduled", Job.kind == "schedule", Job.status == "completed")).all()
        queue = [{"id": p.id, "title": (p.script or {}).get("title") or p.title,
                  "publication_at": j.payload["publication_at"]} for p, j in rows
                 if datetime.fromisoformat(j.payload["publication_at"]) > datetime.now(timezone.utc)]
        return sorted(queue, key=lambda item: datetime.fromisoformat(item["publication_at"]))


@app.post("/api/studio/from-topic")
def start_from_topic(body: TopicInput):
    if not settings.openai_api_key:
        raise HTTPException(422, "Для сценария заполните OPENAI_API_KEY")
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(422, "Введи тему ролика")
    with session() as db:
        project = Project(title=topic, brief=body.notes, status="script_generating")
        db.add(project)
        db.flush()
        enqueue(db, project, "script", {**body.model_dump(), "topic": topic})
        return serialize(project)


@app.post("/api/studio/from-reference")
def start_from_reference(reference_id: str, brief: str = ""):
    if not settings.openai_api_key:
        raise HTTPException(422, "Для сценария заполните OPENAI_API_KEY")
    with session() as db:
        ref = db.get(Reference, reference_id)
        if not ref or not (ref.analysis or ref.asset_id):
            raise HTTPException(422, "Выберите загруженный референс")
        asset = db.get(Asset, ref.asset_id) if ref.asset_id else None
        project = Project(title=asset.name if asset else ref.notes[:200],
                          brief=brief[:10000] or "Сохрани тему, смысл и подачу выбранного референса. Создай оригинальный Reels.",
                          status="script_generating")
        db.add(project)
        db.flush()
        enqueue(db, project, "script", {"reference_id": ref.id, "pipeline": True})
        return serialize(project)


@app.post("/api/projects", status_code=201)
def create_project(body: CreateProject):
    with session() as db:
        project = Project(**body.model_dump())
        db.add(project)
        db.flush()
        event(db, project.id, "created", brief=project.brief)
        return serialize(project)


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    with session() as db:
        project = project_or_404(db, project_id)
        jobs = db.scalars(select(Job).where(Job.project_id == project_id).order_by(Job.created_at.desc())).all()
        script_job = next((j for j in jobs if j.kind == "script"), None)
        ref_id = script_job.payload.get("reference_id") if script_job else None
        ref = db.get(Reference, ref_id) if ref_id else None
        source = source_info(db.get(Asset, ref.asset_id)) if ref and ref.asset_id else None
        cover_path = str(Path(project.render_path).with_suffix(".jpg")) if project.render_path else None
        if cover_path and not (settings.data_dir / cover_path).is_file():
            cover_path = None
        return {**serialize(project), "reference_file": source, "cover_path": cover_path, "can_change_background": studio.can_swap_background(project),
                "assets": [serialize(a) for a in db.scalars(select(Asset).where(Asset.project_id == project_id)).all()],
                "jobs": [serialize(j) for j in jobs],
                "events": [serialize(e) for e in db.scalars(select(Event).where(Event.project_id == project_id).order_by(Event.created_at.desc()).limit(50)).all()]}


@app.post("/api/projects/{project_id}/generate")
def generate(project_id: str, body: GenerateInput):
    if not settings.openai_api_key:
        raise HTTPException(422, "Добавьте OPENAI_API_KEY в .env")
    with session() as db:
        project = project_or_404(db, project_id)
        if project.post_id:
            raise HTTPException(409, "Для новой версии опубликованного выпуска создайте отдельный выпуск")
        if body.reference_id:
            ref = db.get(Reference, body.reference_id)
            if not ref or not (ref.analysis or ref.asset_id):
                raise HTTPException(422, "Для сценария по референсу нужен загруженный видеофайл или готовый разбор")
        payload = body.model_dump()
        previous = db.scalar(select(Job).where(Job.project_id == project.id, Job.kind == "script")
                             .order_by(Job.created_at.desc()).limit(1))
        if previous and previous.payload.get("topic") and not body.reference_id:
            payload.update({key: previous.payload[key] for key in ("topic", "notes", "hook_style")})
        if previous and previous.payload.get("pipeline") and body.reference_id:
            payload["pipeline"] = True
        result = enqueue(db, project, "script", payload)
        if body.feedback:
            event(db, project.id, "script_feedback", text=body.feedback)
        project.mode, project.edit, project.render_path = None, None, None
        project.status = "script_generating"
        return result


@app.put("/api/projects/{project_id}/script")
def save_script(project_id: str, body: Script):
    with session() as db:
        project = project_or_404(db, project_id)
        require_idle(db, project.id)
        if project.post_id:
            raise HTTPException(409, "Выпуск уже передан в публикацию")
        if project.script:
            event(db, project.id, "script_version", script=project.script)
        project.script, project.status = body.model_dump(), "script_review"
        project.mode, project.edit, project.render_path = None, None, None
        return serialize(project)


@app.post("/api/projects/{project_id}/script-review")
def review_script(project_id: str, body: ScriptReviewInput):
    with session() as db:
        project = project_or_404(db, project_id)
        require_idle(db, project.id)
        if project.status != "script_review" or not (project.script or {}).get("pipeline_job_id"):
            raise HTTPException(409, "Дождись нового сценария для редактирования")
        narrations = [text.strip() for text in body.scenes]
        if any(not text or len(text) > 5000 for text in narrations):
            raise HTTPException(422, "Каждой сцене нужна реплика длиной до 5000 символов")
        try:
            updated = studio.edit_for_review(project, body.cover_hook_text, narrations, body.selected_hook)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        event(db, project.id, "script_version", script=project.script)
        project.script = updated
        return serialize(project)


@app.post("/api/projects/{project_id}/approve-script")
def approve_script(project_id: str):
    with session() as db:
        project = project_or_404(db, project_id)
        require_idle(db, project.id)
        if project.status != "script_review" or not project.script:
            raise HTTPException(409, "Сначала подготовьте сценарий для согласования")
        project.status = "script_approved"
        event(db, project.id, "script_approved", script=project.script)
        return serialize(project)


@app.post("/api/projects/{project_id}/production")
def production(project_id: str, body: ProductionInput):
    with session() as db:
        project = project_or_404(db, project_id)
        require_idle(db, project.id)
        if project.status not in ("script_approved", "production"):
            raise HTTPException(409, "Сначала утвердите сценарий и раскадровку")
        if body.mode in ("avatar", "mixed"):
            if not all((settings.heygen_api_key, settings.heygen_avatar_id, settings.heygen_voice_id)):
                raise HTTPException(422, "Аватар ещё не настроен. Заполните параметры HeyGen в .env")
            auto_render = body.auto_render and body.mode == "avatar"
            if body.local_edit and (not auto_render or
                    not (body.background_mode or (project.script or {}).get("pipeline_job_id"))):
                raise HTTPException(422, "Для локального монтажа выбери аватар и укажи фон")
            if body.local_edit and body.background_mode in ("auto_scene", "ai_generate"):
                raise HTTPException(422, "Для монтажа без OpenAI выбери готовый фон или хромакей")
            if auto_render and not body.local_edit and not settings.openai_api_key:
                raise HTTPException(422, "Для выбранного монтажа или фона нужен OPENAI_API_KEY")
            if body.background_mode and not auto_render:
                raise HTTPException(422, "Для смены фона выберите аватар и автоматический монтаж")
            if body.background_mode in ("preset", "auto_scene", "greenscreen"):
                validate_background(db, body.background, local_only=body.background_mode == "greenscreen")
            kind = "pipeline_video" if auto_render and (body.background_mode or (project.script or {}).get("pipeline_job_id")) else "avatar"
            enqueue(db, project, kind, {"auto_render": auto_render, "background_mode": body.background_mode,
                                        "background": body.background, "local_edit": body.local_edit})
        project.mode, project.status = body.mode, "production"
        event(db, project.id, "production_selected", mode=body.mode)
        return serialize(project)


def validate_background(db, name, *, local_only=False):
    try:
        spec = preset_spec(name, studio.available_backgrounds(db))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if local_only and "url" in spec:
        raise HTTPException(422, "Для Remotion выберите цвет или локальный файл")


@app.get("/api/backgrounds")
def list_backgrounds():
    with session() as db:
        return [{"id": key, "label": spec.get("description", key), "local": "url" not in spec}
                for key, spec in studio.available_backgrounds(db).items()]


@app.post("/api/projects/{project_id}/background")
def change_background(project_id: str, body: BackgroundInput):
    with session() as db:
        project = project_or_404(db, project_id)
        require_idle(db, project.id)
        if project.status not in ("video_review", "video_approved") or not studio.can_swap_background(project):
            raise HTTPException(409, "Сначала соберите видео в режиме хромакея")
        validate_background(db, body.background, local_only=True)
        return enqueue(db, project, "pipeline_background", body.model_dump())


@app.post("/api/projects/{project_id}/generate-clip")
def generate_clip(project_id: str, body: GenerateClip):
    if not settings.heygen_api_key:
        raise HTTPException(422, "Добавьте HEYGEN_API_KEY для генерации видеовставок")
    with session() as db:
        project = project_or_404(db, project_id)
        if project.status != "production":
            raise HTTPException(409, "Сначала утвердите сценарий и выберите способ съёмки")
        return enqueue(db, project, "insert", body.model_dump())


@app.post("/api/projects/{project_id}/edit")
def edit(project_id: str, body: EditInput):
    if not settings.openai_api_key:
        raise HTTPException(422, "Добавьте OPENAI_API_KEY")
    with session() as db:
        project = project_or_404(db, project_id)
        if project.status not in ("production", "edit_review", "video_review"):
            raise HTTPException(409, "Сначала подготовьте материалы выпуска")
        if not db.scalar(select(Asset).where(Asset.project_id == project.id,
                Asset.kind.in_(["footage", "avatar"])).limit(1)):
            raise HTTPException(422, "Загрузите свою запись или дождитесь аватара")
        if (project.mode == "avatar" and project.status == "video_review" and project.render_path
                and not project.edit and (project.script or {}).get("pipeline_job_id")):
            return enqueue(db, project, "pipeline_edit", {"feedback": body.feedback})
        project.render_path = None
        return enqueue(db, project, "edit", body.model_dump())


@app.put("/api/projects/{project_id}/edit")
def save_edit(project_id: str, body: EditPlan):
    with session() as db:
        project = project_or_404(db, project_id)
        require_idle(db, project.id)
        if project.status not in ("production", "edit_review", "video_review"):
            raise HTTPException(409, "Монтаж недоступен на этом этапе")
        assets = db.scalars(select(Asset).where(Asset.project_id == project.id)).all()
        validate_assets(body, {a.id: a for a in assets})
        project.edit, project.status, project.render_path = body.model_dump(), "edit_review", None
        return serialize(project)


@app.post("/api/projects/{project_id}/render")
def render(project_id: str):
    with session() as db:
        project = project_or_404(db, project_id)
        if project.status not in ("edit_review", "video_review") or not project.edit:
            raise HTTPException(409, "Сначала подготовьте монтажный план")
        return enqueue(db, project, "render")


@app.post("/api/projects/{project_id}/approve-video")
def approve_video(project_id: str):
    with session() as db:
        project = project_or_404(db, project_id)
        require_idle(db, project.id)
        if project.status != "video_review" or not project.render_path:
            raise HTTPException(409, "Сначала соберите и просмотрите ролик")
        project.status = "video_approved"
        event(db, project.id, "video_approved", render_path=project.render_path)
        return serialize(project)


@app.post("/api/projects/{project_id}/schedule")
def schedule(project_id: str, body: ScheduleInput):
    if not settings.metricool_token or not settings.metricool_user_id:
        raise HTTPException(422, "Настройте Metricool API в .env")
    publication = datetime.fromisoformat(body.publication_at)
    if publication.tzinfo is None or publication <= datetime.now(timezone.utc):
        raise HTTPException(422, "Нужна будущая дата с часовым поясом")
    public_url(body.public_video_url)
    with session() as db:
        project = project_or_404(db, project_id)
        if project.status != "video_approved" or project.post_id:
            raise HTTPException(409, "Сначала утвердите готовое видео")
        return enqueue(db, project, "schedule", body.model_dump())


@app.post("/api/projects/{project_id}/publication-status")
def publication_status(project_id: str):
    with session() as db:
        project = project_or_404(db, project_id)
        if not project.post_id:
            raise HTTPException(409, "Выпуск ещё не передан в Metricool")
        return enqueue(db, project, "publication_status")


def source_info(asset):
    if asset is None:
        return None
    return {"name": asset.name, "duration": asset.duration,
            "kind": "image" if Path(asset.path).suffix.lower() in {".jpg", ".jpeg"} else "video"}


@app.get("/api/references")
def references():
    with session() as db:
        rows = db.execute(select(Reference, Asset).outerjoin(Asset, Reference.asset_id == Asset.id))
        return [{**serialize(r), "file": source_info(asset)} for r, asset in rows]


@app.post("/api/references", status_code=201)
def add_reference(body: ReferenceInput):
    with session() as db:
        if body.asset_id:
            asset = db.get(Asset, body.asset_id)
            if not asset or asset.kind != "reference":
                raise HTTPException(422, "Загрузите видео как референс")
        reference = Reference(**body.model_dump())
        db.add(reference)
        db.flush()
        return serialize(reference)


@app.post("/api/assets", status_code=201)
async def upload(file: UploadFile = File(...), kind: str = Form(...), project_id: str = Form("")):
    if kind not in ("reference", "footage", "insert", "background"):
        raise HTTPException(422, "Неизвестный тип материала")
    with session() as db:
        if kind not in ("reference", "background"):
            project = project_or_404(db, project_id)
            require_idle(db, project_id)
            if project.status not in ("production", "edit_review", "video_review"):
                raise HTTPException(409, "Загрузка записи доступна после утверждения сценария и выбора съёмки")
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    image_reference = kind == "reference" and suffix in (".jpg", ".jpeg")
    if kind == "background" and suffix not in MEDIA:
        raise HTTPException(422, "Фон должен быть файлом JPG, PNG или MP4")
    image_background = kind == "background" and suffix in {".jpg", ".jpeg", ".png"}
    is_image = image_reference or image_background
    if not is_image and suffix not in (".mp4", ".mov", ".webm", ".m4v"):
        raise HTTPException(422, "Для референса выберите JPEG, MOV или MP4")
    asset_id = new_id()
    relative = f"uploads/{asset_id}{suffix}"
    target = settings.data_dir / relative
    total = 0
    try:
        with target.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_upload_mb * 1024 * 1024:
                    raise HTTPException(413, "Файл превышает MAX_UPLOAD_MB")
                output.write(chunk)
        try:
            metadata = probe(target)
        except RuntimeError as exc:
            if kind == "background":
                raise HTTPException(422, "Файл фона не удалось прочитать") from exc
            raise
        if not any(stream["codec_type"] == "video" for stream in metadata["streams"]):
            raise ValueError("В файле нет видеодорожки")
        expected_codec = "png" if suffix == ".png" else "mjpeg"
        if is_image and metadata["streams"][0].get("codec_name") != expected_codec:
            raise HTTPException(422, "Содержимое изображения не соответствует формату файла")
        duration = 0 if is_image else float(metadata["format"]["duration"])
        if not is_image and not 0 < duration <= 3600:
            raise ValueError("Загрузите видео длительностью до одного часа")
        with session() as db:
            if kind not in ("reference", "background"):
                project = project_or_404(db, project_id)
                require_idle(db, project_id)
                if project.status not in ("production", "edit_review", "video_review"):
                    raise HTTPException(409, "Этап выпуска изменился во время загрузки")
                project.edit, project.render_path, project.status = None, None, "production"
            asset = Asset(id=asset_id, project_id=project_id or None, name=(file.filename or "Видео")[:200],
                          kind=kind, path=relative, duration=duration)
            db.add(asset)
            db.flush()
            return serialize(asset)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@app.post("/api/assets/{asset_id}/analyze")
def analyze(asset_id: str):
    if not settings.openai_api_key:
        raise HTTPException(422, "Добавьте OPENAI_API_KEY")
    with session() as db:
        asset = db.get(Asset, asset_id)
        if not asset:
            raise HTTPException(404, "Материал не найден")
        for job in db.scalars(select(Job).where(Job.kind == "analyze", Job.status.in_(ACTIVE))).all():
            if job.payload.get("asset_id") == asset_id:
                raise HTTPException(409, "Анализ этого файла уже выполняется")
        return enqueue(db, None, "analyze", {"asset_id": asset_id})


@app.get("/api/jobs")
def jobs():
    with session() as db:
        return [serialize(j) for j in db.scalars(select(Job).order_by(Job.created_at.desc()).limit(100)).all()]


@app.post("/api/jobs/{job_id}/retry")
def retry(job_id: str):
    with session() as db:
        job = db.get(Job, job_id)
        if not job or job.status not in ("failed", "attention"):
            raise HTTPException(409, "Задача недоступна для повтора")
        if job.kind in ("avatar", "insert", "schedule") and not (job.result or {}).get("video_id"):
            raise HTTPException(409, "Проверьте кабинет сервиса. Для неопределённого внешнего запроса автоматический повтор отключён")
        if job.project_id:
            other = db.scalar(select(Job).where(Job.project_id == job.project_id,
                Job.id != job.id, Job.status.in_(ACTIVE)).limit(1))
            if other:
                raise HTTPException(409, "У выпуска уже есть активная задача")
        job.status, job.error = "queued", ""
        return serialize(job)


@app.post("/api/jobs/{job_id}/dismiss")
def dismiss(job_id: str):
    with session() as db:
        job = db.get(Job, job_id)
        if not job or job.status not in ("failed", "attention"):
            raise HTTPException(409, "Можно закрыть только завершившуюся с ошибкой задачу")
        job.status = "dismissed"
        event(db, job.project_id, "job_dismissed", job_id=job.id)
        return serialize(job)


@app.get("/media/{relative:path}")
def media(relative: str):
    path = (settings.data_dir / relative).resolve()
    if not path.is_relative_to(settings.data_dir) or relative.split("/")[0] not in ("uploads", "renders"):
        raise HTTPException(404)
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)
