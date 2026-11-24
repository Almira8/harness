"use strict";
const el = id => document.getElementById(id);
let current;
let connections;
let busy = false;
let previewKey = "";
const labels = {
  draft: "Черновик", script_generating: "Подготовка сценария", script_review: "Проверь сценарий и раскадровку",
  script_approved: "Сценарий согласован", production: "Подготовка материалов", edit_review: "Монтажный план готов",
  video_review: "Посмотри готовый ролик перед утверждением", video_approved: "Видео утверждено", scheduled: "Публикация запланирована"
};
async function api(path, body) {
  const form = body instanceof FormData;
  const response = await fetch("/api/" + path, body === undefined ? {} : {
    method: "POST", headers: form ? {} : {"Content-Type":"application/json"}, body: form ? body : JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw Error(typeof data.detail === "string" ? data.detail : "Проверь заполненные поля");
  return data;
}
function route(action) { return "projects/" + current.id + "/" + action; }
function panel(name) {
  for (const id of ["upload","lineup","source","changes","schedule"]) el(id).hidden = id !== name;
}
function addText(parent, tag, content) {
  const node = document.createElement(tag);
  node.textContent = content;
  parent.append(node);
  return node;
}
function fileLabel(file) {
  const kind = file.kind === "image" ? "Фото · 1 кадр" : "Видео · " + Math.round(file.duration) + " с";
  return kind + " · " + file.name;
}
el("file").onchange = () => { if (el("file").files.length) el("saved").value = ""; };
el("saved").onchange = () => { if (el("saved").value) el("file").value = ""; };

async function selectProject(id) {
  const data = await api("projects/" + id);
  current = data;
  localStorage.setItem("studioProject", id);
  el("reference-file").textContent = data.reference_file ? fileLabel(data.reference_file) : "";
  const job = data.jobs[0];
  const active = data.jobs.some(j => ["queued","waiting","running","attention"].includes(j.status));
  const scriptStage = ["draft","script_review","script_generating"].includes(data.status);
  el("agree").disabled = active || !["draft","script_review","script_approved","production","edit_review"].includes(data.status);
  el("revise").disabled = active || !(scriptStage || ["edit_review","video_review"].includes(data.status));
  el("approve").disabled = active || !["video_review","video_approved"].includes(data.status);
  el("title").textContent = data.script?.title || data.title;
  el("state").textContent = job?.error && ["attention","failed"].includes(job.status) ? job.error :
    (active ? ({script:"Разбираем референс и пишем сценарий…", avatar:"Готовим аватар…",pipeline_video:"Готовим сцены, субтитры и монтаж…",edit:"Монтируем…",pipeline_edit:"Пересобираем монтаж…",render:"Собираем MP4…",schedule:"Передаём в расписание…"}[job?.kind] || "Обработка…") : labels[data.status]);
  const evidence = data.jobs.find(j => j.kind === "script")?.result?.reference_evidence;
  const key = JSON.stringify([id,data.status,data.script,data.render_path,job?.status,evidence]);
  if (key === previewKey) return;
  previewKey = key;
  const box = el("preview");
  box.replaceChildren();
  if (data.render_path) {
    const video = document.createElement("video");
    video.controls = true;
    video.src = "/media/" + data.render_path;
    box.append(video);
  }
  if (evidence) {
    const details = document.createElement("details");
    addText(details, "summary", "Текст из референса");
    if (evidence.topic) {
      addText(details, "p", (evidence.topic_supported === false ? "Тема не подтверждена · " : "Тема · ") + evidence.topic);
    }
    addText(details, "h3", "Речь");
    if (!evidence.transcript.length) addText(details, "p", "Речь не распознана.");
    for (const item of evidence.transcript) addText(details, "p", item.start.toFixed(1) + " с · " + item.text);
    addText(details, "h3", "Надписи в кадре");
    if (evidence.ocr_warning) addText(details, "p", evidence.ocr_warning);
    if (!evidence.screen_text.length) addText(details, "p", "Надписи не распознаны.");
    for (const item of evidence.screen_text) {
      const line = addText(details, "p", item.time.toFixed(1) + " с · " + item.text);
      line.style.whiteSpace = "pre-line";
    }
    box.append(details);
  }
  if (data.script && data.status !== "script_generating") {
    const details = document.createElement("details");
    details.open = data.status === "script_review";
    addText(details,"summary","Посекундная раскадровка");
    addText(details,"p","Это план ролика. Точное время уточняется после записи голоса.");
    const timeline = document.createElement("ol");
    timeline.className = "storyboard";
    for (const second of data.script.seconds) {
      const row = addText(timeline,"li","");
      addText(row,"strong", second.second + "–" + (second.second + 1) + " с");
      const fields = document.createElement("dl");
      const entries = [
        ["В кадре", second.action || "Кадр не указан"],
        ["Голос", second.speech || "Пауза"],
        ["Надпись", second.screen_text === undefined || second.screen_text === null
          ? "Не указана" : second.screen_text || "Без надписи"],
        ["Движение", second.animation || "Без нового движения"],
      ];
      for (const [label, value] of entries) {
        addText(fields,"dt",label);
        addText(fields,"dd",value);
      }
      row.append(fields);
    }
    details.append(timeline);
    box.append(details);
    const narration = document.createElement("details");
    addText(narration,"summary","Текст ведущей целиком");
    addText(narration,"p",data.script.scenes.map(s => s.narration).join(" "));
    box.append(narration);
  }
  if (job && ["failed","attention"].includes(job.status)) {
    const retry = addText(box,"button","Повторить задачу");
    retry.onclick = () => run(async () => { await api("jobs/"+job.id+"/retry",{}); });
  }
}
async function refreshList(preferred) {
  const items = await api("projects");
  el("projects").replaceChildren(...items.map(p => new Option(p.script?.title || p.title,p.id)));
  el("picker").hidden = items.length < 2;
  if (!items.length) {
    el("state").textContent = "Загрузи референс, чтобы подготовить первый сценарий.";
    return;
  }
  const id = items.find(p => p.id === preferred)?.id || items[0].id;
  el("projects").value = id;
  await selectProject(id);
}
async function run(fn) {
  if (busy) return;
  busy = true;
  el("notice").textContent = "";
  try { await fn(); }
  catch (e) { el("notice").textContent = e.message; }
  finally {
    try { if (current) await selectProject(current.id); }
    catch(e) { el("notice").textContent = e.message; }
    busy = false;
  }
}
function on(id, fn, submit=false) {
  el(id).addEventListener(submit ? "submit" : "click", e => {
    e.preventDefault();
    run(fn);
  });
}
on("reference", async () => {
  if (!el("upload").hidden) return panel();
  const refs = (await api("references")).filter(r => r.asset_id || r.analysis);
  el("saved").replaceChildren(new Option("Выбрать…",""), ...refs.map(r => new Option(r.file ? fileLabel(r.file) : r.notes,r.id)));
  panel("upload");
});
on("uploadForm", async () => {
  const file = el("file").files[0];
  let id = el("saved").value;
  if (file) {
    const form = new FormData();
    form.set("file",file); form.set("kind","reference");
    el("notice").textContent = "Загрузка референса…";
    const asset = await api("assets",form);
    id = (await api("references",{asset_id:asset.id,notes:el("notes").value.trim() || file.name})).id;
    el("saved").add(new Option(file.name,id,true,true));
    el("file").value = "";
  }
  if (!id) throw Error("Выбери JPEG, MOV, MP4 или сохранённый референс.");
  const project = await api("studio/from-reference?reference_id="+encodeURIComponent(id)+"&brief="+encodeURIComponent(el("notes").value),{});
  panel();
  el("notice").textContent = "";
  el("uploadForm").reset();
  await refreshList(project.id);
},true);
on("agree",async () => {
  if (current.status === "edit_review") { await api(route("render"),{}); return; }
  if (current.status === "draft") { await api(route("generate"),{}); return; }
  if (current.status === "script_review") await api(route("approve-script"),{});
  panel("source");
});
el("mode").onchange = () => { el("footage").hidden = el("mode").value !== "own"; };
on("sourceForm",async () => {
  const mode = el("mode").value;
  if (mode === "own" && !el("footage").files.length) throw Error("Выбери свою видеозапись.");
  await api(route("production"),{mode,auto_render:mode === "avatar"});
  if (mode === "own") {
    const form = new FormData();
    form.set("kind","footage"); form.set("project_id",current.id); form.set("file",el("footage").files[0]);
    await api("assets",form);
    await api(route("edit"),{auto_render:true});
  }
  panel();
},true);
on("revise",async () => { panel("changes"); el("feedback").focus(); });
on("changesForm",async () => {
  const feedback = el("feedback").value.trim();
  if (!feedback) throw Error("Напиши, что изменить.");
  if (["video_review","edit_review"].includes(current.status)) await api(route("edit"),{feedback,auto_render:true});
  else await api(route("generate"),{feedback,reference_id:current.jobs.find(j => j.kind === "script")?.payload.reference_id || null});
  panel(); el("feedback").value = "";
},true);
on("approve",async () => {
  if (current.status === "video_review") await api(route("approve-video"),{});
  if (connections.metricool) panel("schedule");
  else {panel(); el("notice").textContent = "Видео утверждено. Для публикации по расписанию подключи сервис публикации.";}
});
on("scheduleForm",async () => {
  await api(route("schedule"),{publication_at:el("when").value+":00+03:00",public_video_url:el("url").value});
  panel();
},true);
on("queue",async () => {
  if (!el("lineup").hidden) return panel();
  const slots = await api("queue");
  const box = el("slots"); box.replaceChildren();
  if (!slots.length) addText(box,"p","Запланированных роликов пока нет.");
  for (const s of slots) addText(box,"p",new Date(s.publication_at).toLocaleString("ru-RU",{timeZone:"Europe/Moscow"})+" МСК · "+s.title);
  panel("lineup");
});
el("projects").onchange = () => run(async () => { panel(); await selectProject(el("projects").value); });
run(async () => { connections = await api("status"); await refreshList(localStorage.getItem("studioProject")); });
setInterval(async () => {
  if (busy || !current || document.hidden || !current.jobs.some(j => ["queued","running","waiting"].includes(j.status))) return;
  await run(async () => {});
},3000);
