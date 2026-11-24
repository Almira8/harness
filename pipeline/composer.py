import difflib
import re

from app.media import probe
from schemas.pipeline import Phrase, ScriptPlan
from .common import digest, dump, ffmpeg, read, whisper
from .visuals import local_asset


def stamp(t):
    cs = max(0,round(t*100))
    return f"{cs//360000}:{cs//6000%60:02}:{cs//100%60:02}.{cs%100:02}"


def clean(text):
    return re.sub(r"[{}\\\r\n]", " ", text).strip()


def scene_times(plan, words, duration):
    normalize = lambda s: re.sub(r"[^\w]", "", s.casefold())
    tokens = [s.spoken_text.split() for s in plan.scenes]
    expected = [normalize(w) for line in tokens for w in line]
    observed = [normalize(w.text) for w in words]
    anchors = {0:0.0,len(expected):duration}
    for block in difflib.SequenceMatcher(None,expected,observed,autojunk=False).get_matching_blocks():
        for n in range(block.size):
            if block.a+n:
                anchors[block.a+n] = words[block.b+n].start
    times = [0.0]
    index = 0
    for line in tokens[:-1]:
        index += len(line)
        a = max(i for i in anchors if i<=index)
        b = min(i for i in anchors if i>=index)
        times.append(anchors[a] if a==b else anchors[a]+(anchors[b]-anchors[a])*(index-a)/(b-a))
    return times+[duration]


def compose(directory):
    raw = directory/"4_heygen_raw.mp4"
    plan = ScriptPlan.model_validate(read(directory/"3_script_plan.json"))
    metadata = probe(raw)
    video = next(s for s in metadata["streams"] if s["codec_type"]=="video")
    w,h = video["width"],video["height"]
    duration = float(metadata["format"]["duration"])
    cache = directory/("words-"+digest(raw)+".json")
    if not cache.exists():
        audio = directory/"4_voice.wav"
        ffmpeg("-i",raw,"-vn","-ar",16000,"-ac",1,audio)
        words = whisper(audio,"word")
        if not words:
            raise ValueError("Whisper не вернул пословных таймкодов")
        dump(cache,[p.model_dump() for p in words])
    words = [Phrase.model_validate(p) for p in read(cache)]
    times = scene_times(plan,words,duration)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,DejaVu Sans,{round(w*.047)},&H00FFFFFF,&H0000D8FF,&H00202020,&H90000000,-1,0,0,0,100,100,0,0,1,3,0,2,50,50,{round(h*.12)},1
Style: Overlay,DejaVu Sans,{round(w*.043)},&H00FFFFFF,&H00FFFFFF,&H00354D37,&H00354D37,-1,0,0,0,100,100,0,0,3,9,0,8,60,60,{round(h*.09)},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    def line(layer,start,end,style,text):
        events.append(f"Dialogue: {layer},{stamp(start)},{stamp(end)},{style},,0,0,0,,{text}")
    for i in range(0,len(words),3):
        group = words[i:i+3]
        for j,word in enumerate(group):
            parts = [clean(p.text) for p in group]
            if plan.subtitle_style=="highlight":
                parts[j] = r"{\c&H00D8FF&}"+parts[j]+r"{\rSub}"
            line(1,word.start,word.end,"Sub"," ".join(parts))
    for scene,start,end in zip(plan.scenes,times,times[1:]):
        if scene.overlay_text:
            line(0,start,end,"Overlay",clean(scene.overlay_text))
    (directory/"5_captions.ass").write_text(header+"\n".join(events)+"\n")
    fps = 30
    bumps = [f"max(0,1-abs(on-{round(t*fps)})/8)" for t in times[1:-1]]
    zoom = "1+0.035*min(1,"+("+".join(bumps) or "0")+")"
    vf = f"fps={fps},zoompan=z='{zoom}':x='iw/2-iw/zoom/2':y='ih/2-ih/zoom/2':d=1:s={w}x{h}:fps={fps},ass=5_captions.ass"
    args = ["-i",raw]
    if plan.music.asset:
        args += ["-stream_loop",-1,"-i",local_asset(plan.music.asset),"-filter_complex",
                 f"[1:a]volume={plan.music.volume}[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
                 "-map","0:v:0","-map","[a]"]
    else:
        args += ["-map","0:v:0","-map","0:a:0"]
    temp = directory/"5_rendering.mp4"
    ffmpeg(*args,"-vf",vf,"-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",
           "-c:a","aac","-movflags","+faststart",temp,cwd=directory)
    output = directory/"5_final_output.mp4"
    temp.replace(output)
    return output
