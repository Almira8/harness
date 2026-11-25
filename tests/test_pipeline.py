import shutil

from app.config import settings
from app.media import probe
from pipeline import composer, originality, scriptwriter, visuals
from schemas.pipeline import ReferenceAnalysis
from pipeline.edit_director import Callout, EditDirector
from pipeline.common import dump, ffmpeg, read
from schemas.pipeline import Phrase, ScreenText, ScriptPlan


def plan():
    scene = {"scene_id": 1, "spoken_text": "Первый пример", "camera_framing": "closeup",
             "background_type": "color", "background_prompt_or_asset": "studio_dark",
             "overlay_text": "Пример", "b_roll_query": None}
    return ScriptPlan.model_validate({"title":"Проверка", "caption":"Описание",
        "scenes":[scene, dict(scene, scene_id=2, spoken_text="Второй пример", camera_framing="half_body")],
        "subtitle_style":"highlight","music":{"asset":None,"volume":0.1}, "cover_hook_text":"Где теряется время на монтаже"})


def unchanged_spoken_edit(data):
    return scriptwriter.SpokenEdit.model_validate({"scenes": [
        {"scene_id": scene["scene_id"], "spoken_text": scene["spoken_text"]}
        for scene in data["plan"]["scenes"] if scene["scene_id"] in data["requested_scenes"]
    ]})


def idea():
    return {"topic_problem": "Зрителю трудно разобраться в редакторе.",
            "core_insight": "Раздельные дорожки делают процесс управляемым.",
            "hook_formula": "Вопрос о результате", "narrative_formula": "Проблема → решение → пример",
            "visual_rhythm": "Перебивка на демонстрации"}


def test_writer_receives_only_semantic_fields(tmp_path, monkeypatch):
    dump(tmp_path / "2_analysis.json", idea())
    dump(tmp_path / "1_extracted.json", {"transcript": [{"text": "verbatim-source"}]})
    dump(tmp_path / "3_script_plan.json", {"previous": "old-phrasing"})

    def generate(prompt, data, schema, **kwargs):
        assert "verbatim-source" not in str(data) and "old-phrasing" not in str(data)
        if schema is scriptwriter.SpokenEdit:
            return unchanged_spoken_edit(data)
        assert data["meaning"] == idea()
        if schema is scriptwriter.TopicCheck:
            return schema(supported=True, correction="")
        assert kwargs["temperature"] == 0.75
        assert "новый жизненный пример" in prompt
        return plan()

    monkeypatch.setattr(scriptwriter, "vision", generate)
    scriptwriter.write(tmp_path, "studio_dark", feedback="Сделай объяснение короче.",
                       needs_rewrite=originality.check_draft(tmp_path))
    assert read(tmp_path / "3_script_plan.json")["scenes"][0]["spoken_text"] == "Первый пример"


def test_render_resume_and_cached_director_props(tmp_path,monkeypatch):
    for name in ("heygen_api_key","heygen_voice_id","heygen_avatar_id"):
        monkeypatch.setattr(settings,name,"test-only")
    source = tmp_path/"fixture.mp4"
    ffmpeg("-f","lavfi","-i","color=s=180x320:r=30","-f","lavfi","-i","sine=frequency=440",
           "-t",2,"-pix_fmt","yuv420p",source)
    draft = plan()
    dump(tmp_path/"3_script_plan.json",draft.model_dump())
    requests = []
    def request(method,url,**kwargs):
        requests.append(method)
        if method=="POST":
            assert len(kwargs["json"]["video_inputs"])==2
            return {"video_id":"known"}
        return {"status":"completed","video_url":"fixture"}
    monkeypatch.setattr(visuals,"request",request)
    monkeypatch.setattr(visuals,"download_video",lambda url,dest:shutil.copyfile(source,dest))
    visuals.render(tmp_path)
    draft.scenes[0].overlay_text="Новая плашка"
    dump(tmp_path/"3_script_plan.json",draft.model_dump())
    visuals.render(tmp_path)
    assert requests==["POST","GET"]
    monkeypatch.setattr(composer,"whisper",lambda *a: [
        Phrase(start=i*.5,end=(i+1)*.5,text=w) for i,w in enumerate("Первый пример Второй пример".split())])
    def direct(plan, words, duration, assets, feedback, previous):
        assert words[0].word == "Первый" and words[1].startSec == 0.5
        return EditDirector(zoom_events=[],b_roll=[],callouts=[
            Callout(startSec=0,endSec=1,title=plan.scenes[0].overlay_text,subtitle=None)])
    monkeypatch.setattr(composer,"direct",direct)
    def render(directory, props):
        output = directory/"5_final_output.mp4"
        shutil.copyfile(directory/props.avatar,output)
        return output
    monkeypatch.setattr(composer,"render",render)
    final = composer.compose(tmp_path)
    props = read(tmp_path/"remotion_props.json")
    assert props["callouts"][0]["title"] == "Новая плашка"
    assert float(probe(final)["format"]["duration"]) >= 1.9
    monkeypatch.setattr(composer,"whisper",lambda *a:1/0)
    monkeypatch.setattr(composer,"direct",lambda *a:1/0)
    props["callouts"][0]["title"] = "Ручная правка"
    dump(tmp_path/"remotion_props.json",props)
    composer.compose(tmp_path,render_only=True)
    assert read(tmp_path/"remotion_props.json") == props
    assert requests==["POST","GET"]


def test_silent_reference_sends_ordered_individual_frames(tmp_path, monkeypatch):
    from pipeline import analyzer, extractor

    reference = tmp_path / "silent.mov"
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=15", "-t", 5, reference)
    monkeypatch.setattr(extractor, "read_frames", lambda *_: (
        [ScreenText(time=0, text="Монтаж Reels")], None))
    extractor.extract(reference, tmp_path)

    def vision(prompt, data, schema, images=()):
        if schema is analyzer.SourceReview:
            assert data["observations"][0]["kind"] == "screen_text"
            return schema(supported=True, evidence_index=0)
        times = [sample["time"] for sample in data["samples"]]
        assert times == sorted(times)
        assert times[0] == 0 and times[-1] > 3
        assert len(images) == len(times) and all(image.is_file() for image in images)
        assert not data["transcript"]
        assert "опирайся ТОЛЬКО на текст с кадров" in prompt
        assert schema is ReferenceAnalysis
        return schema(**idea())

    monkeypatch.setattr(analyzer, "vision", vision)
    analyzer.analyze(tmp_path)
    assert read(tmp_path / "2_analysis.json") == idea()


def test_studio_heygen_uses_configured_engine_and_keeps_remote_id(tmp_path, monkeypatch):
    from app import providers

    for name in ("heygen_api_key", "heygen_voice_id", "heygen_avatar_id"):
        monkeypatch.setattr(settings, name, "test-only")
    monkeypatch.setattr(settings, "heygen_engine", "avatar_v")
    source = tmp_path / "fixture.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=s=180x320:r=30", "-f", "lavfi", "-i",
           "sine=frequency=440", "-t", 1, source)
    dump(tmp_path / "3_script_plan.json", plan().model_dump())
    requests = []

    def request(method, path, payload=None, idempotency_key=None):
        requests.append((method, path))
        if method == "POST":
            assert payload["engine"] == {"type": "avatar_v"}
            assert payload["script"] == "Первый пример\n\nВторой пример"
            assert payload["background"] == {"type": "color", "value": "#20352F"}
            assert idempotency_key
            return {"video_id": "known"}
        return {"status": "completed", "video_url": "fixture"}

    monkeypatch.setattr(providers, "heygen_request", request)
    monkeypatch.setattr(visuals, "download_video", lambda url, dest: shutil.copyfile(source, dest))
    render = visuals.render(tmp_path, studio_title="Тест")
    render.unlink()
    visuals.render(tmp_path, studio_title="Тест")
    assert requests == [("POST", "videos"), ("GET", "videos/known"), ("GET", "videos/known")]


def test_director_cues_fit_timeline_and_available_assets():
    from pipeline.edit_director import Insert

    cues = [
        Insert(startSec=3, endSec=8, src="clip.mp4", animation="fade"),
        Insert(startSec=0, endSec=2, src="missing.mp4", animation="fade"),
        Insert(startSec=2, endSec=4, src="card.jpg", animation="fade"),
    ]
    normalized = composer.normalize_cues(cues, 6, {"clip.mp4": {"duration": 1}, "card.jpg": {}})
    assert [(cue.src, cue.startSec, cue.endSec) for cue in normalized] == [
        ("card.jpg", 2, 4), ("clip.mp4", 4, 5),
    ]


def test_abstract_idea_review_rejects_unrelated_script(tmp_path, monkeypatch):
    import pytest

    dump(tmp_path / "2_analysis.json", idea())
    calls = []

    def generate(prompt, data, schema, **kwargs):
        calls.append(schema)
        if schema is ScriptPlan:
            assert "Подменённая идея" not in str(data)
            return plan()
        if schema is scriptwriter.SpokenEdit:
            return unchanged_spoken_edit(data)
        return schema(supported=False, correction="Подменённая идея")

    monkeypatch.setattr(scriptwriter, "vision", generate)
    with pytest.raises(ValueError, match="Черновик не сохранён"):
        scriptwriter.write(tmp_path, "studio_dark")
    assert calls == [ScriptPlan, scriptwriter.SpokenEdit, scriptwriter.TopicCheck] * 2
    assert not (tmp_path / "3_script_plan.json").exists()


def test_reference_without_speech_or_ocr_stops_before_analysis(tmp_path, monkeypatch):
    import pytest

    from pipeline import extractor

    reference = tmp_path / "silent.mov"
    ffmpeg("-f", "lavfi", "-i", "color=s=160x90:r=10", "-f", "lavfi", "-i",
           "anullsrc=r=16000:cl=mono", "-t", 1, reference)
    monkeypatch.setattr(extractor, "whisper", lambda *_: [
        Phrase(start=0, end=1, text="А... 123! Вот тут монтаж"),
    ])
    monkeypatch.setattr(extractor, "read_frames", lambda *_: ([], None))
    with pytest.raises(ValueError, match=r"В этом видеофайле не найдена речь и текст на экране"):
        extractor.extract(reference, tmp_path)
    assert not (tmp_path / "1_extracted.json").exists()


def test_writer_retries_only_once_for_copied_trigram(tmp_path, monkeypatch):
    import pytest

    dump(tmp_path / "2_analysis.json", idea())
    dump(tmp_path / "1_extracted.json", {
        "transcript": [{"text": "Убираем паузы вручную и обозначаем громкие участки отдельным цветом"}],
    })
    attempts = []

    def generate(prompt, data, schema, **kwargs):
        assert schema is ScriptPlan
        assert "Убираем паузы вручную" not in str(data)
        attempts.append(data.get("revision"))
        draft = plan()
        draft.scenes[0].spoken_text = "Убираем паузы вручную."
        return draft

    monkeypatch.setattr(scriptwriter, "vision", generate)
    with pytest.raises(ValueError, match="Черновик не сохранён"):
        scriptwriter.write(tmp_path, "studio_dark", needs_rewrite=originality.check_draft(tmp_path))
    assert attempts[0] is None and "новую бытовую ситуацию" in attempts[1]
    assert len(attempts) == 2
    assert not (tmp_path / "3_script_plan.json").exists()


def test_jaccard_rejects_reordered_vocabulary_above_threshold():
    assert originality.overlap(["субтитры предпросмотр дорожка новый"],
                               "дорожка эффект предпросмотр субтитры")
    assert not originality.overlap(["кадр композиция анимация"],
                                   "звук музыка дорожка кадр")


def test_same_reference_cannot_return_previous_narration(tmp_path, monkeypatch):
    dump(tmp_path / "2_analysis.json", idea())
    previous = plan()
    narration = " ".join(scene.spoken_text for scene in previous.scenes)
    calls = []

    def generate(prompt, data, schema, **kwargs):
        assert narration not in str(data)
        calls.append(schema)
        if schema is scriptwriter.SpokenEdit:
            return unchanged_spoken_edit(data)
        if schema is scriptwriter.TopicCheck:
            return schema(supported=True, correction="")
        if len(calls) == 1:
            return previous
        assert "слишком похож" in data["revision"]
        rewritten = plan()
        rewritten.scenes[0].spoken_text = "Сначала определим, что зритель должен заметить."
        return rewritten

    monkeypatch.setattr(scriptwriter, "vision", generate)
    scriptwriter.write(tmp_path, "studio_dark", needs_rewrite=originality.check_draft(tmp_path, [narration]))
    assert calls == [ScriptPlan, ScriptPlan, scriptwriter.SpokenEdit, scriptwriter.TopicCheck]
    assert read(tmp_path / "3_script_plan.json")["scenes"][0]["spoken_text"].startswith("Сначала")


def test_spoken_editor_retries_only_scene_with_cliche_and_preserves_visuals(monkeypatch):
    from schemas.pipeline import ScreenBeat

    draft = plan()
    first = draft.scenes[0]
    first.screen_beats = [
        ScreenBeat(speech="Первый", action="Ведущая смотрит в камеру", screen_text="", animation=""),
        ScreenBeat(speech="", action="Крупно экран", screen_text="Пример", animation="Появляется надпись"),
        ScreenBeat(speech="пример", action="Держим кадр", screen_text="", animation=""),
    ]
    original = draft.model_dump()
    brand_voice = (scriptwriter.CONFIG / "brand_voice.md").read_text()
    calls = []

    def edit(prompt, data, schema):
        assert schema is scriptwriter.SpokenEdit
        assert "Монолог 1" in prompt
        calls.append(data["requested_scenes"])
        if len(calls) == 1:
            return schema(scenes=[
                scriptwriter.SpokenLine(scene_id=1, spoken_text="НЕ СЕКРЕТ, ЧТО это работает."),
                scriptwriter.SpokenLine(scene_id=2, spoken_text="Смотри, как это выглядит."),
            ])
        return schema(scenes=[scriptwriter.SpokenLine(scene_id=1, spoken_text="Вот как я это делаю.")])

    monkeypatch.setattr(scriptwriter, "vision", edit)
    scriptwriter.edit_spoken_text(draft, brand_voice)
    assert calls == [{1: [], 2: []}, {1: ["не секрет что"]}]
    assert first.spoken_text == "Вот как я это делаю."
    assert draft.scenes[1].spoken_text == "Смотри, как это выглядит."
    assert first.screen_beats[1].speech == ""
    assert " ".join(beat.speech for beat in first.screen_beats).split() == first.spoken_text.split()

    edited = draft.model_dump()
    for before, after in zip(original["scenes"], edited["scenes"]):
        before.pop("spoken_text")
        after.pop("spoken_text")
        for beat in (before["screen_beats"] or []) + (after["screen_beats"] or []):
            beat.pop("speech")
    assert edited == original


def test_topic_plan_uses_selected_cover_and_humanized_opening(tmp_path, monkeypatch):
    from schemas.pipeline import HookOption

    draft = plan()
    draft.scenes[0].spoken_text = "Сложно разобрать слова? Попробуй отдельную дорожку."
    draft.selected_hook = 1
    draft.alternative_hooks = [
        HookOption(spoken_hook="Смотри на шкалу.", cover_hook_text="Где теряются важные слова"),
        HookOption(spoken_hook="Сложно разобрать слова?", cover_hook_text="Как спасти тихую дорожку"),
        HookOption(spoken_hook="Титры снова опаздывают?", cover_hook_text="Почему текст приходит слишком поздно"),
    ]

    def generate(prompt, data, schema, **kwargs):
        if schema is ScriptPlan:
            assert data["meaning"] == {"topic": "Монтаж", "hook_style": "Ошибка"}
            return draft
        if schema is scriptwriter.SpokenEdit:
            return schema(scenes=[
                scriptwriter.SpokenLine(scene_id=1, spoken_text="Речь еле слышно? Вынеси голос на отдельную дорожку."),
                scriptwriter.SpokenLine(scene_id=2, spoken_text="Теперь прибавь громкость."),
            ], cover_hook_text="Как спасти тихую дорожку")
        assert data["draft"]["scenes"][0]["spoken_text"].startswith("Речь еле слышно?")
        return schema(supported=True, correction="")

    monkeypatch.setattr(scriptwriter, "vision", generate)
    scriptwriter.generate_from_topic("Монтаж", hook_style="mistake", directory=tmp_path)
    saved = read(tmp_path / "3_script_plan.json")
    assert saved["selected_hook"] == 1
    assert saved["alternative_hooks"][1]["spoken_hook"] == "Речь еле слышно?"
    assert saved["alternative_hooks"][1]["cover_hook_text"] == saved["cover_hook_text"]
    assert saved["cover_hook_duration_sec"] == 2.5
    assert not (tmp_path / "1_extracted.json").exists()


def test_reference_rejects_coffee_theme_when_speech_explains_editing(tmp_path, monkeypatch):
    from pipeline import analyzer
    from schemas.pipeline import Extraction

    dump(tmp_path / "1_extracted.json", Extraction(
        duration=4, preview="preview.mp4", audio="audio.wav", contact_sheet="contact_sheet.jpg",
        transcript=[Phrase(start=0, end=4, text="Перенеси субтитры на дорожку выше и укороти паузы.")],
        screen_text=[ScreenText(time=0, text="Coffee")], cuts=[], words_per_minute=120,
    ).model_dump())
    reviews = []

    def model(prompt, data, schema, images=()):
        if schema is analyzer.SourceReview:
            assert data["observations"][0]["kind"] == "speech"
            assert data["observations"][1]["text"] == "Coffee"
            text = str(data["candidate"])
            supported = "кофе" not in text
            reviews.append(supported)
            return schema(supported=supported, evidence_index=0 if supported else -1)
        if not reviews:
            return schema(**{**idea(), "topic_problem": "Зритель хочет заварить кофе."})
        return schema(**idea())

    monkeypatch.setattr(analyzer, "vision", model)
    analyzer.analyze(tmp_path)
    assert read(tmp_path / "2_analysis.json") == idea()
    wrong_script = plan()
    wrong_script.scenes[0].spoken_text = "Сегодня завариваем кофе."
    assert not analyzer.draft_check(tmp_path)(wrong_script)
    assert reviews == [False, True, False]


def test_quiet_audio_is_raised_before_whisper(tmp_path):
    from pipeline.extractor import extract_audio

    source = tmp_path / "quiet.mov"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:r=48000", "-af", "volume=-15dB", "-t", 1, source)
    result = tmp_path / "audio.wav"
    extract_audio(source, result)
    metadata = probe(result)["streams"][0]
    assert metadata["sample_rate"] == "16000" and metadata["channels"] == 1
    log = ffmpeg("-i", result, "-af", "volumedetect", "-f", "null", "-")
    import re

    peak = float(re.search(r"max_volume:\s*(-?[\d.]+) dB", log)[1])
    assert -24 < peak <= -3
