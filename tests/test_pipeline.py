import shutil

from app.config import settings
from app.media import probe
from pipeline import composer, scriptwriter, visuals
from pipeline.common import dump, ffmpeg, read
from schemas.pipeline import Phrase, ScriptPlan


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


def test_render_resume_and_real_composition(tmp_path,monkeypatch):
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
    final = composer.compose(tmp_path)
    assert "Новая плашка" in (tmp_path/"5_captions.ass").read_text()
    assert float(probe(final)["format"]["duration"]) >= 1.9
    monkeypatch.setattr(composer,"whisper",lambda *a:1/0)
    composer.compose(tmp_path)
    assert requests==["POST","GET"]
