from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


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
    topic_and_tools: str
    pain_and_hook: str
    actionable_steps: list[str]
    visual_structure: str

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


def validate_cover_text(text):
    if not 4 <= len(text.split()) <= 8:
        raise ValueError("Заголовок обложки должен содержать от 4 до 8 слов")
    return text


CoverText = Annotated[str, Field(min_length=1, max_length=50), AfterValidator(validate_cover_text)]
HookStyle = Literal["auto", "pain", "mistake", "case", "numbers"]


class HookOption(Record):
    spoken_hook: str = Field(min_length=1, max_length=180)
    cover_hook_text: CoverText


class ScriptPlan(Record):
    title: str
    caption: str
    scenes: list[SceneItem] = Field(min_length=1, max_length=50)
    music: Music
    subtitle_style: Literal["highlight", "minimal"]
    cover_hook_text: CoverText | None = None
    cover_hook_duration_sec: float = Field(default_factory=lambda: 2.5, ge=0.25, le=10)
    alternative_hooks: list[HookOption] | None = None
    selected_hook: int | None = Field(default=None, ge=0, le=2)

    @model_validator(mode="after")
    def unique_scenes(self):
        if len({s.scene_id for s in self.scenes}) != len(self.scenes):
            raise ValueError("scene_id должен быть уникальным")
        return self
