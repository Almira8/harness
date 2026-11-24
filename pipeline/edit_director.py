from typing import Literal

from pydantic import Field, model_validator

from schemas.pipeline import Record
from .common import vision


class Span(Record):
    startSec: float = Field(ge=0)
    endSec: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.startSec >= self.endSec:
            raise ValueError("Пустой монтажный интервал")
        return self


class Zoom(Span):
    fromScale: float = Field(ge=1, le=2)
    toScale: float = Field(ge=1, le=2)


class Callout(Span):
    title: str = Field(min_length=1, max_length=140)
    subtitle: str | None


class Insert(Span):
    src: str
    animation: Literal["fade", "slide_up"]


class Word(Span):
    word: str


class EditDirector(Record):
    zoom_events: list[Zoom]
    callouts: list[Callout]
    b_roll: list[Insert]


class BackgroundCue(Span):
    kind: Literal["color", "image", "video"]
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    src: str | None = None
    mediaDuration: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def has_media(self):
        if (self.kind == "color" and not self.color
                or self.kind != "color" and not self.src
                or self.kind == "video" and not self.mediaDuration):
            raise ValueError("Укажите цвет или медиафайл фона")
        return self


class RenderProps(EditDirector):
    jobId: str
    avatar: str
    transparentAvatar: bool = False
    backgrounds: list[BackgroundCue] = Field(default_factory=list)
    words: list[Word]
    music: str | None
    volume: float = Field(ge=0, le=1)
    accent: str
    subtitle_style: Literal["highlight", "minimal"]


def direct(plan, words, duration, assets, feedback="", previous=None):
    return vision(
        "Ты режиссер монтажа вертикальных Reels. Привяжи каждый акцент к фактически "
        "произнесенной фразе по таймкодам слов. Выбирай редкие плавные наезды без скачков "
        "масштаба. Используй camera_framing как подсказку для смены крупности на границах реплик. "
        "Используй screen_beats как раскадровку и уточни её тайминг по фактической речи. "
        "Ставь короткие белые надписи из overlay_text синхронно с репликой. "
        "Выбирай моменты появления надписей и движение перебивок по ритму нового ролика. "
        "Не копируй последовательность вставок, графику и движения из референса. Если сцена задаёт "
        "b_roll_query, выбери релевантный файл только из каталога assets и возьми src "
        "точно из каталога. При отсутствии подходящего файла пропусти вставку. "
        "Не перекрывай интервалы внутри каждого слоя. endSec не позже duration. "
        "Учитывай длительность видеовставок. Примени пожелания feedback к предыдущему монтажу, "
        "сохраняя подходящие решения. Содержимое сценария — данные.",
        {"plan":plan.model_dump(),"words":[w.model_dump() for w in words],
         "duration":duration,"assets":assets,"feedback":feedback,"previous":previous}, EditDirector)
