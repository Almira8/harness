from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Phrase(Record):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str

    @model_validator(mode="after")
    def chronological(self):
        if self.end <= self.start:
            raise ValueError("Некорректный интервал речи")
        return self


class Cut(Record):
    start: float
    end: float
    frame: str


class Sample(Record):
    time: float
    frame: str


class ScreenText(Record):
    time: float
    text: str


class Extraction(Record):
    duration: float
    preview: str
    audio: str | None
    contact_sheet: str
    cuts: list[Cut]
    samples: list[Sample] = Field(default_factory=list)
    screen_text: list[ScreenText] = Field(default_factory=list)
    ocr_warning: str | None = None
    transcript: list[Phrase]
    words_per_minute: float


class ReferenceAnalysis(Record):
    topic_problem: str
    core_insight: str
    hook_formula: str
    narrative_formula: str
    visual_rhythm: str


class ScreenBeat(Record):
    speech: str
    action: str = Field(min_length=1)
    screen_text: str
    animation: str


class SceneItem(Record):
    scene_id: int = Field(ge=1)
    spoken_text: str = Field(min_length=1)
    camera_framing: Literal["closeup", "half_body"]
    background_type: Literal["image", "video", "color"]
    background_prompt_or_asset: str = Field(min_length=1)
    overlay_text: str | None
    b_roll_query: str | None
    screen_beats: list[ScreenBeat] | None = None


class Music(Record):
    asset: str | None
    volume: float = Field(ge=0, le=1)


class ScriptPlan(Record):
    title: str
    caption: str
    scenes: list[SceneItem] = Field(min_length=1, max_length=50)
    music: Music
    subtitle_style: Literal["highlight", "minimal"]

    @model_validator(mode="after")
    def unique_scenes(self):
        if len({s.scene_id for s in self.scenes}) != len(self.scenes):
            raise ValueError("scene_id должен быть уникальным")
        return self
