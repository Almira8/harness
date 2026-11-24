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


class Extraction(Record):
    duration: float
    preview: str
    audio: str | None
    contact_sheet: str
    cuts: list[Cut]
    transcript: list[Phrase]
    words_per_minute: float


class ReferenceAnalysis(Record):
    core_message: str = Field(min_length=1)
    hook_mechanism: str = Field(min_length=1)
    narrative_arc: str = Field(min_length=1)
    pacing_and_editing: str = Field(min_length=1)
    original_transcript: list[Phrase]


class SceneItem(Record):
    scene_id: int = Field(ge=1)
    spoken_text: str = Field(min_length=1)
    camera_framing: Literal["closeup", "half_body"]
    background_type: Literal["image", "video", "color"]
    background_prompt_or_asset: str = Field(min_length=1)
    overlay_text: str | None
    b_roll_query: str | None


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
