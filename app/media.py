import base64
import json
import subprocess
from pathlib import Path

from .config import settings


def run(command, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Ошибка {command[0]} — {result.stderr[-1500:]}")
    return result.stdout


def probe(path: Path):
    metadata = json.loads(run([
        "ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)
    ]))
    return metadata


def transcribe(path: Path, client):
    metadata = probe(path)
    if not any(stream["codec_type"] == "audio" for stream in metadata["streams"]):
        return {"text": "", "segments": [], "note": "В исходнике нет звуковой дорожки"}
    # Split long inputs to stay below the transcription API file-size limit.
    duration = float(metadata["format"]["duration"])
    all_segments = []
    for offset in range(0, int(duration) + 1, 600):
        if offset >= duration:
            break
        audio_path = settings.data_dir / "work" / f"{path.stem}-{offset}.mp3"
        try:
            run(["ffmpeg", "-y", "-ss", str(offset), "-i", str(path), "-t", "600",
                 "-vn", "-ac", "1", "-ar", "16000", "-b:a", "48k", str(audio_path)])
            with audio_path.open("rb") as audio:
                result = client.audio.transcriptions.create(
                    model=settings.transcription_model, file=audio,
                    response_format="verbose_json", timestamp_granularities=["segment"],
                )
            data = result.model_dump()
            for segment in data.get("segments", []):
                all_segments.append({"start": segment["start"] + offset,
                                     "end": segment["end"] + offset, "text": segment["text"]})
        finally:
            audio_path.unlink(missing_ok=True)
    return {"text": " ".join(item["text"] for item in all_segments), "segments": all_segments}


def sample_frames(path: Path, duration: float):
    frames = []
    image = path.suffix.lower() in {".jpg", ".jpeg"}
    count = 1 if image or duration == 0 else min(24, max(6, round(duration / 2)))
    for index in range(count):
        position = min(max(duration - 0.1, 0), duration * index / count)
        seek = ["-ss", str(position)] if position > 0 else []
        result = subprocess.run([
            "ffmpeg", "-v", "error", *seek, "-i", str(path), "-map", "0:v:0",
            "-frames:v", "1", "-vf", "scale=768:-2", "-c:v", "mjpeg",
            "-f", "image2pipe", "pipe:1",
        ], capture_output=True, timeout=120)
        if result.returncode:
            raise ValueError("Не удалось прочитать изображение или видеокадр. Проверьте исходный файл.")
        if result.stdout:
            frames.append({"type": "input_image", "image_url": "data:image/jpeg;base64," +
                           base64.b64encode(result.stdout).decode()})
    if not frames:
        raise ValueError("В референсе не удалось прочитать ни одного кадра.")
    return frames
