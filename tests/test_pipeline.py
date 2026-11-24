import shutil

from app.config import settings
from app.media import probe
from pipeline import composer, scriptwriter, visuals
from pipeline.edit_director import Callout, EditDirector
from pipeline.common import dump, ffmpeg, read
from schemas.pipeline import Phrase, ScreenText, ScriptPlan


def plan():
    scene = {"scene_id": 1, "spoken_text": "Первый пример", "camera_framing": "closeup",
             "background_type": "color", "background_prompt_or_asset": "studio_dark",
             "overlay_text": "Пример", "b_roll_query": None}
    return ScriptPlan.model_validate({"title":"Проверка", "caption":"Описание",
        "scenes":[scene, dict(scene, scene_id=2, spoken_text="Второй пример", camera_framing="half_body")],
        "subtitle_style":"highlight","music":{"asset":None,"volume":0.1}})


def test_writer_receives_only_semantic_fields(tmp_path,monkeypatch):
    dump(tmp_path/"2_analysis.json", {"core_message":"Смысл","hook_mechanism":"Вопрос",
        "narrative_arc":"Демонстрация","pacing_and_editing":"Две склейки",
        "original_transcript":[{"start":0.0,"end":1.0,"text":"verbatim-source"}]})
    def generate(prompt,data,schema):
        assert "original_transcript" not in data["meaning"]
        assert "verbatim-source" not in str(data)
        return plan()
    monkeypatch.setattr(scriptwriter,"vision",generate)
    scriptwriter.write(tmp_path,"studio_dark")
    assert read(tmp_path/"3_script_plan.json")["scenes"][0]["spoken_text"]=="Первый пример"


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

    def vision(prompt, data, schema, images):
        times = [sample["time"] for sample in data["samples"]]
        assert times == sorted(times)
        assert times[0] == 0 and times[-1] > 3
        assert len(images) == len(times) and all(image.is_file() for image in images)
        assert not data["transcript"]
        assert "опирайся ТОЛЬКО на текст с кадров" in prompt
        return schema(core_message="Показ интерфейса", hook_mechanism="Действие",
                      narrative_arc="Демонстрация", pacing_and_editing="Без речи",
                      original_transcript=[])

    monkeypatch.setattr(analyzer, "vision", vision)
    analyzer.analyze(tmp_path)
    assert read(tmp_path / "2_analysis.json")["core_message"] == "Показ интерфейса"


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


def test_screen_evidence_rejects_unrelated_script(tmp_path, monkeypatch):
    dump(tmp_path / "2_analysis.json", {
        "core_message": "Монтаж Reels", "hook_mechanism": "Титр", "narrative_arc": "Инструкция",
        "pacing_and_editing": "Запись экрана", "original_transcript": [], "topic_supported": True,
    })
    dump(tmp_path / "1_extracted.json", {
        "duration": 47, "words_per_minute": 0, "transcript": [],
        "screen_text": [{"time": 0.0, "text": "Как монтировать Reels с ИИ"}],
    })
    calls = []

    def generate(prompt, data, schema):
        assert data["screen_text"][0]["text"] == "Как монтировать Reels с ИИ"
        calls.append(schema)
        if schema is ScriptPlan:
            return plan()
        return schema(supported=False, correction="Верни тему монтажа Reels.")

    monkeypatch.setattr(scriptwriter, "vision", generate)
    import pytest

    with pytest.raises(ValueError, match="Не удалось написать сценарий"):
        scriptwriter.write(tmp_path, "studio_dark")
    assert calls == [ScriptPlan, scriptwriter.TopicCheck, ScriptPlan, scriptwriter.TopicCheck]
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


def test_writer_rewrites_copied_hook_and_checks_new_version(tmp_path, monkeypatch):
    copied = "Как сделать ролик из записи экрана"
    source = copied + " и быстро добавить субтитры."
    dump(tmp_path / "2_analysis.json", {
        "core_message": "Субтитры к записи экрана", "hook_mechanism": "Вопрос",
        "narrative_arc": "Показ процесса", "pacing_and_editing": "Экран",
        "original_transcript": [{"start": 0.0, "end": 4.0, "text": source}],
        "topic_supported": True,
    })
    dump(tmp_path / "1_extracted.json", {
        "duration": 4, "words_per_minute": 120, "transcript": [], "screen_text": [],
    })
    calls = []

    def generate(prompt, data, schema):
        calls.append(schema)
        if schema is scriptwriter.TopicCheck:
            assert "построчном пересказе" in prompt
            assert data["draft"]["narration"][0] == "Запись готова? Теперь сделаем речь читаемой."
            return schema(supported=True, correction="")
        draft = plan()
        if len(calls) == 1:
            draft.scenes[0].spoken_text = copied
        else:
            assert "из шести и более слов" in data["revision"]
            draft.scenes[0].spoken_text = "Запись готова? Теперь сделаем речь читаемой."
        return draft

    monkeypatch.setattr(scriptwriter, "vision", generate)
    scriptwriter.write(tmp_path, "studio_dark")
    assert calls == [ScriptPlan, ScriptPlan, scriptwriter.TopicCheck]
    assert read(tmp_path / "3_script_plan.json")["scenes"][0]["spoken_text"].startswith("Запись готова?")
