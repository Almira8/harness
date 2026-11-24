import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Second(StrictModel):
    second: int = Field(ge=0)
    speech: str
    action: str
    animation: str
    sound: str


class Scene(StrictModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    narration: str
    screen_text: str = Field(max_length=100)
    graphic: str = Field(max_length=220)
    layout: Literal["presenter", "split", "fullscreen"]
    animation: Literal["rise", "slide", "pop", "fade"]
    filming: str
    materials: list[str]


class Script(StrictModel):
    title: str
    hook_options: list[str] = Field(min_length=3, max_length=8)
    rationale: str
    duration: int = Field(ge=10, le=90)
    caption: str = Field(max_length=2200)
    facts_to_verify: list[str]
    scenes: list[Scene] = Field(min_length=2, max_length=30)
    seconds: list[Second]

    @model_validator(mode="after")
    def check_timeline(self):
        previous_end = 0.0
        for scene in self.scenes:
            if abs(scene.start - previous_end) > 0.02 or scene.end <= scene.start:
                raise ValueError("Сцены должны идти подряд, без пробелов и пересечений")
            previous_end = scene.end
        if abs(previous_end - self.duration) > 0.02:
            raise ValueError("Сцены должны покрывать всю длительность ролика")
        if [beat.second for beat in self.seconds] != list(range(self.duration)):
            raise ValueError("Нужна раскадровка каждой секунды от 0 до duration − 1")
        return self


class Clip(StrictModel):
    asset_id: str
    start: float = Field(ge=0)
    source_start: float = Field(ge=0)
    duration: float = Field(gt=0)
    layout: Literal["presenter", "split", "fullscreen"]
    muted: bool
    zoom: float = Field(ge=1, le=1.3)


class Caption(StrictModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(max_length=100)


class Overlay(StrictModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    title: str = Field(max_length=100)
    body: str = Field(max_length=220)
    animation: Literal["rise", "slide", "pop", "fade"]


class EditPlan(StrictModel):
    duration: float = Field(ge=5, le=120)
    clips: list[Clip] = Field(min_length=1, max_length=100)
    captions: list[Caption]
    overlays: list[Overlay]
    notes: str

    @model_validator(mode="after")
    def check_timeline(self):
        previous_end = 0.0
        for clip in self.clips:
            if abs(clip.start - previous_end) > 0.04:
                raise ValueError("Видеофрагменты должны идти подряд")
            previous_end = clip.start + clip.duration
        if abs(previous_end - self.duration) > 0.04:
            raise ValueError("Клипы должны покрывать всю длительность")
        for item in [*self.captions, *self.overlays]:
            if item.end <= item.start or item.end > self.duration + 0.04:
                raise ValueError("Вставка выходит за пределы ролика")
        return self


class CreateProject(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    brief: str = Field(min_length=1, max_length=10000)
    questions: str = Field(default="", max_length=20000)


class ReferenceInput(StrictModel):
    url: str = Field(default="", max_length=2000)
    notes: str = Field(min_length=1, max_length=10000)
    asset_id: str | None = None


class GenerateInput(StrictModel):
    feedback: str = Field(default="", max_length=10000)
    research: bool = False


class EditInput(StrictModel):
    feedback: str = Field(default="", max_length=10000)


class ProductionInput(StrictModel):
    mode: Literal["own", "avatar", "mixed"]


class GenerateClip(StrictModel):
    prompt: str = Field(min_length=10, max_length=8000)
    duration: int = Field(default=5, ge=5, le=15)


class ScheduleInput(StrictModel):
    publication_at: str
    public_video_url: str = Field(max_length=2000)


def validate_assets(plan: EditPlan, assets: dict):
    for clip in plan.clips:
        asset = assets.get(clip.asset_id)
        if not asset or asset.kind not in ("footage", "avatar", "insert"):
            raise ValueError("Монтаж использует недоступное видео")
        if not math.isfinite(asset.duration) or clip.source_start + clip.duration > asset.duration + 0.05:
            raise ValueError("Монтаж выходит за пределы исходного видео")
