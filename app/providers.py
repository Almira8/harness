import ipaddress
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .config import settings


def public_url(url):
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Нужна публичная HTTPS-ссылка без логина и пароля")
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("Ссылка должна вести на публичный сервер")
    return url


def download_video(url, destination):
    public_url(url)
    with httpx.Client(timeout=120) as http:
        for _ in range(4):
            with http.stream("GET", url) as response:
                if response.is_redirect:
                    url = str(response.url.join(response.headers["location"]))
                    public_url(url)
                    continue
                response.raise_for_status()
                total = 0
                partial = destination.with_suffix(".part")
                try:
                    with partial.open("wb") as output:
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > settings.max_upload_mb * 1024 * 1024:
                                raise ValueError("Видео превышает MAX_UPLOAD_MB")
                            output.write(chunk)
                    partial.replace(destination)
                finally:
                    partial.unlink(missing_ok=True)
                return
    raise ValueError("Слишком много перенаправлений при получении видео")


def heygen_request(method, path, payload=None, idempotency_key=None):
    if not settings.heygen_api_key:
        raise ValueError("Добавьте HEYGEN_API_KEY в .env")
    headers = {"x-api-key": settings.heygen_api_key}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    response = httpx.request(method, "https://api.heygen.com/v3/" + path,
                            headers=headers, json=payload, timeout=120)
    if not response.is_success:
        raise ValueError(f"HeyGen вернул HTTP {response.status_code}. Проверьте доступ и настройки API")
    data = response.json()
    return data.get("data", data)


def create_avatar(project, job_id):
    if not settings.heygen_avatar_id or not settings.heygen_voice_id:
        raise ValueError("Укажите HEYGEN_AVATAR_ID и HEYGEN_VOICE_ID после создания аватара")
    return heygen_request("POST", "videos", {
        "type": "avatar", "avatar_id": settings.heygen_avatar_id,
        "voice_id": settings.heygen_voice_id,
        "script": " ".join(scene["narration"] for scene in project.script["scenes"]),
        "title": project.title, "engine": {"type": settings.heygen_engine},
        "resolution": "1080p", "aspect_ratio": "9:16",
    }, job_id)


def create_insert(payload, job_id):
    return heygen_request("POST", "models/videos", {
        "model": "heygen-video-1", "mode": "text_to_video",
        "prompt": payload["prompt"], "duration": payload["duration"],
        "resolution": "1080p", "aspect_ratio": "9:16",
    }, job_id)


def metricool_request(method, path, payload=None):
    if not settings.metricool_token or not settings.metricool_user_id:
        raise ValueError("Добавьте METRICOOL_TOKEN и METRICOOL_USER_ID в .env")
    response = httpx.request(method, "https://app.metricool.com/api/v2/" + path,
        headers={"X-Mc-Auth": settings.metricool_token},
        params={"userId": settings.metricool_user_id, "blogId": settings.metricool_blog_id},
        json=payload, timeout=120)
    if not response.is_success:
        raise ValueError(f"Metricool вернул HTTP {response.status_code}. Проверьте доступ к API и аккаунт")
    return response.json()


def schedule_post(project, payload):
    publication = datetime.fromisoformat(payload["publication_at"])
    if publication.tzinfo is None or publication <= datetime.now(timezone.utc):
        raise ValueError("Укажите будущую дату со смещением часового пояса")
    from zoneinfo import ZoneInfo
    return metricool_request("POST", "scheduler/posts", {
        "publicationDate": {"dateTime": publication.astimezone(ZoneInfo("Europe/Moscow")).strftime(
            "%Y-%m-%dT%H:%M:%S"), "timezone": "Europe/Moscow"},
        "text": project.script["caption"], "providers": [{"network": "instagram"}],
        "media": [public_url(payload["public_video_url"])], "saveExternalMediaFiles": True,
        "autoPublish": True, "instagramData": {"type": "REEL"},
    })
