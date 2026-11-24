import shutil

from app.config import settings
from app.media import probe
from pipeline import composer, scriptwriter, visuals
from pipeline.edit_director import Callout, EditDirector
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
    def direct(plan, words, duration, assets):
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
