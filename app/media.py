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
    for index in range(1 if duration == 0 else 6):
        position = min(max(duration - 0.1, 0), duration * index / 6)
        frame_path = settings.data_dir / "work" / f"{path.stem}-frame-{index}.jpg"
        try:
            run(["ffmpeg", "-y", "-ss", str(position), "-i", str(path),
                 "-frames:v", "1", "-vf", "scale=480:-2", str(frame_path)])
            frames.append({"type": "input_image", "image_url": "data:image/jpeg;base64," +
                           base64.b64encode(frame_path.read_bytes()).decode()})
        finally:
            frame_path.unlink(missing_ok=True)
    return frames
