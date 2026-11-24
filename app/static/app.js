const content = document.querySelector("#content");
const title = document.querySelector("#page-title");
const dialog = document.querySelector("#create-dialog");
let currentView = "projects";
let currentProject = null;
let connectionStatus = {};
let refreshing = false;
let unsavedChanges = false;

const stages = {
  draft: "Задание",
  script_generating: "Сценарий готовится",
  script_review: "Проверка сценария",
  script_approved: "Сценарий утверждён",
  production: "Материалы",
  edit_review: "План монтажа",
  video_review: "Проверка видео",
  video_approved: "Видео утверждено",
  scheduled: "В календаре",
};
const jobNames = {
  script: "Сценарий и раскадровка",
  analyze: "Анализ видео",
  avatar: "Аватар HeyGen",
  insert: "Генерация видеовставки",
  edit: "Монтажный план",
  render: "Сборка MP4",
  schedule: "Планирование публикации",
  publication_status: "Статус публикации",
};
const jobStates = {
  queued: "В очереди",
  running: "В работе",
  waiting: "Ожидает HeyGen",
  completed: "Готово",
  failed: "Ошибка",
  attention: "Нужна проверка",
  dismissed: "Закрыта",
};

function escapeHtml(value = "") {
  return String(value).replace(
    /[&<>"']/g,
    (character) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[character],
  );
}

function notice(message, success = false) {
  const element = document.querySelector("#notice");
  element.textContent = message;
  element.className = success ? "success" : "";
  element.hidden = false;
}

async function api(path, method = "GET", body) {
  const options = { method };
  if (body instanceof FormData) options.body = body;
  else if (body !== undefined) {
    options.headers = { "Content-Type": "application/json" };
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  const result = await response.json();
  if (!response.ok) {
    const message =
      typeof result.detail === "string"
        ? result.detail
        : JSON.stringify(result.detail);
    throw new Error(message || "Не удалось выполнить запрос");
  }
  return result;
}

async function action(path, body, method = "POST") {
  try {
    await api(path, method, body);
    notice("Сохранено", true);
    await showView(currentView);
  } catch (error) {
    notice(error.message);
  }
}

function jobsMarkup(jobs) {
  return jobs
    .map(
      (job) => `<div class="job">
    <strong>${escapeHtml(jobNames[job.kind] || job.kind)}</strong>
    <span class="status">${escapeHtml(jobStates[job.status] || job.status)}</span>
    ${job.error ? `<p class="warning">${escapeHtml(job.error)}</p>` : ""}
    ${job.kind === "analyze" && job.result?.analysis ? `<details><summary>Разбор видео</summary><p>${escapeHtml(job.result.analysis)}</p></details>` : ""}
    ${
      ["failed", "attention"].includes(job.status)
        ? `<div class="actions">
      <button data-job-retry="${job.id}">Повторить</button>
      <button data-job-dismiss="${job.id}">Закрыть задачу после проверки</button></div>`
        : ""
    }
  </div>`,
    )
    .join("");
}

async function showProjects() {
  title.textContent = "Выпуски";
  const projects = await api("/api/projects");
  content.innerHTML = projects.length
    ? `<div class="cards">${projects
        .map(
          (project) => `
    <article class="card"><span class="status">${escapeHtml(stages[project.status])}</span>
      <h2>${escapeHtml(project.title)}</h2><p>${escapeHtml(project.brief.slice(0, 140))}</p>
      <button data-project="${project.id}">Открыть выпуск →</button>
    </article>`,
        )
        .join("")}</div>`
    : `<div class="empty"><h2>Начни с одной идеи</h2>
    <p>Добавь вопрос аудитории и референсы. Сначала подготовим сценарий и раскадровку, затем ты выберешь способ съёмки.</p>
    <button class="primary" data-create>Создать первый Reels</button></div>`;
}

function scriptMarkup(project) {
  const script = project.script;
  if (!script) return "";
  return `<section class="panel"><div class="section-heading"><h2>${escapeHtml(script.title)}</h2><span class="status">${script.duration} секунд</span></div>
    <p class="muted">${escapeHtml(script.rationale)}</p>
    <details><summary>Варианты хука</summary><ol class="hooks">${script.hook_options.map((hook) => `<li>${escapeHtml(hook)}</li>`).join("")}</ol></details>
    <div class="narration">${script.scenes.map((scene) => `<p>${escapeHtml(scene.narration)}</p>`).join("")}</div>
    ${script.facts_to_verify.length ? `<div class="warning"><strong>Проверить перед съёмкой</strong><ul>${script.facts_to_verify.map((fact) => `<li>${escapeHtml(fact)}</li>`).join("")}</ul></div>` : ""}
    <details open><summary>Посекундная раскадровка</summary><div class="table-wrap"><table><thead><tr><th>Время</th><th>Речь</th><th>Кадр</th><th>Анимация</th><th>Звук</th></tr></thead><tbody>
      ${script.seconds.map((beat) => `<tr><td>${beat.second}–${beat.second + 1} с</td><td>${escapeHtml(beat.speech)}</td><td>${escapeHtml(beat.action)}</td><td>${escapeHtml(beat.animation)}</td><td>${escapeHtml(beat.sound)}</td></tr>`).join("")}
    </tbody></table></div></details>
    <details><summary>Съёмочное задание и материалы</summary>${script.scenes.map((scene) => `<div class="job"><strong>${scene.start}–${scene.end} с</strong><p>${escapeHtml(scene.filming)}</p><p class="muted">${escapeHtml(scene.materials.join(" · "))}</p></div>`).join("")}</details>
    <details><summary>Редактировать структуру сценария</summary><textarea id="script-json" class="json-editor">${escapeHtml(JSON.stringify(script, null, 2))}</textarea><button data-save-script>Сохранить правки</button></details>
    ${project.status === "script_review" ? `<button class="primary" data-project-action="approve-script">Утвердить сценарий и раскадровку</button>` : ""}
  </section>`;
}

function productionMarkup(project) {
  if (
    ![
      "script_approved",
      "production",
      "edit_review",
      "video_review",
      "video_approved",
      "scheduled",
    ].includes(project.status)
  )
    return "";
  const choose = ["script_approved", "production"].includes(project.status);
  return `<section class="panel"><h2>Производство</h2>
    ${
      choose
        ? `<div class="mode-buttons"><button data-mode="own"><strong>Сниму сама</strong><small>Загрузить свою запись</small></button>
      <button data-mode="avatar" ${connectionStatus.heygen ? "" : "disabled"}><strong>Аватар HeyGen</strong><small>${connectionStatus.heygen ? (connectionStatus.openai ? "Собрать ролик до просмотра" : "Генерация по сценарию") : "Нужно подключить аватар"}</small></button>
      <button data-mode="mixed" ${connectionStatus.heygen ? "" : "disabled"}><strong>Смешанный</strong><small>Моя запись и аватар</small></button></div>`
        : ""
    }
    ${project.mode ? `<p class="muted">Выбран режим ${escapeHtml({ own: "собственная съёмка", avatar: "HeyGen", mixed: "смешанный" }[project.mode])}</p>` : ""}
    ${["production", "edit_review", "video_review"].includes(project.status) ? `<form id="upload-form"><label>Видео для монтажа<input name="file" type="file" accept="video/*" required></label><select name="kind"><option value="footage">Моя запись</option><option value="insert">Видеовставка</option></select><div class="actions"><button type="submit">Загрузить</button></div></form>` : ""}
    ${project.assets.map((asset) => `<div class="asset"><span>${escapeHtml(asset.name)} <span class="muted small">${Math.round(asset.duration)} с · ${escapeHtml(asset.kind)}</span></span><a href="/media/${asset.path}" target="_blank" rel="noopener">Просмотр</a></div>`).join("")}
    ${project.status === "production" ? `<details><summary>Сгенерировать видеовставку</summary><form id="clip-form"><label>Описание кадра<textarea name="prompt" required minlength="10" placeholder="Объект, действие, окружение, камера и звук"></textarea></label><label>Длительность, секунды<input name="duration" type="number" min="5" max="15" value="5"></label><button ${connectionStatus.video_generation ? "" : "disabled"}>Генерировать через HeyGen</button><p class="hint">Запрос расходует средства HeyGen API.</p></form></details>` : ""}
    ${["production", "edit_review", "video_review"].includes(project.status) ? `<label>Пожелания или правки к монтажу<textarea id="edit-feedback" rows="2" placeholder="Например, на 8-й секунде укрупнить пример"></textarea></label><div class="actions"><button class="primary" data-project-action="edit">Смонтировать и собрать MP4</button></div>` : ""}
  </section>`;
}

async function showProject(projectId) {
  currentProject = projectId;
  const project = await api("/api/projects/" + projectId);
  title.textContent = project.title;
  const stepIndex = {
    draft: 0,
    script_generating: 0,
    script_review: 1,
    script_approved: 2,
    production: 2,
    edit_review: 3,
    video_review: 4,
    video_approved: 4,
    scheduled: 5,
  }[project.status];
  const active = project.jobs.some((job) =>
    ["queued", "running", "waiting", "attention"].includes(job.status),
  );
  content.innerHTML = `<button class="back" data-view="projects">← Все выпуски</button>
    <ol class="steps">${["Задание", "Сценарий", "Материалы", "Монтаж", "Видео", "Публикация"].map((step, index) => `<li class="${index === stepIndex ? "current" : ""}">${step}</li>`).join("")}</ol>
    <section class="panel"><h2>Задание</h2><p>${escapeHtml(project.brief)}</p>${project.questions ? `<p class="muted">${escapeHtml(project.questions)}</p>` : ""}
      ${
        !project.post_id
          ? `<label>Правки или пожелания<textarea id="feedback" rows="2" placeholder="Что учесть в новой версии сценария"></textarea></label>
      <label class="inline-label"><input id="research" type="checkbox">Исследовать свежие ролики за 72 часа</label>
      <button class="primary" data-generate ${active ? "disabled" : ""}>${project.script ? "Переписать сценарий" : "Подготовить сценарий и раскадровку"}</button>`
          : ""
      }
    </section>${scriptMarkup(project)}${productionMarkup(project)}
    ${
      project.edit
        ? `<section class="panel"><h2>Монтажный план</h2><p>${escapeHtml(project.edit.notes)}</p>
      <p class="muted">${project.edit.clips.length} фрагментов · ${project.edit.overlays.length} вставок · ${project.edit.duration.toFixed(1)} с</p>
      <details><summary>Посмотреть и изменить таймлайн</summary><textarea id="edit-json" class="json-editor">${escapeHtml(JSON.stringify(project.edit, null, 2))}</textarea><button data-save-edit>Сохранить монтажный план</button></details>
      ${["edit_review", "video_review"].includes(project.status) ? `<button class="primary" data-project-action="render" ${active ? "disabled" : ""}>Собрать MP4</button>` : ""}</section>`
        : ""
    }
    ${project.render_path ? `<section class="panel"><h2>Готовый Reels</h2><video controls preload="metadata" src="/media/${project.render_path}"></video><div class="actions"><a href="/media/${project.render_path}" download>Скачать MP4</a>${project.status === "video_review" ? '<button class="primary" data-project-action="approve-video">Утвердить видео</button>' : ""}</div></section>` : ""}
    ${project.status === "video_approved" ? `<section class="panel"><h2>Публикация в Instagram</h2><p>${escapeHtml(project.script.caption)}</p><form id="schedule-form"><label>Публичная постоянная HTTPS-ссылка на готовый MP4<input name="public_video_url" type="url" required placeholder="https://…/reel.mp4"></label><p class="hint">Metricool должен скачать файл. Ссылка localhost недоступна внешнему сервису.</p><label>Время публикации по Москве<input name="publication_at" type="datetime-local" required></label><button class="primary" ${connectionStatus.metricool ? "" : "disabled"}>Запланировать Reels</button></form></section>` : ""}
    ${project.post_id ? `<section class="panel"><h2>Публикация</h2><p>Metricool ID ${escapeHtml(project.post_id)}</p><button data-project-action="publication-status">Проверить статус</button><pre>${escapeHtml(JSON.stringify(project.publication, null, 2))}</pre></section>` : ""}
    ${project.jobs.length ? `<section class="panel"><h2>Задачи выпуска</h2>${jobsMarkup(project.jobs)}</section>` : ""}`;
}

async function showReferences() {
  title.textContent = "Референсы";
  const references = await api("/api/references");
  content.innerHTML = `<div class="grid-two"><section class="panel"><h2>Добавить референс</h2><form id="reference-form">
    <label>Ссылка на ролик<input name="url" type="url" placeholder="https://www.instagram.com/…"></label>
    <label>Что тебе нравится<textarea name="notes" required rows="4" placeholder="Композиция, монтаж, юмор, способ показать результат…"></textarea></label>
    <label>Само видео для анализа<input name="file" type="file" accept="video/*"></label>
    <p class="hint">Ссылка сохраняется как источник. Для разбора речи и кадров загрузи видеофайл.</p>
    <button class="primary">Сохранить референс</button></form></section>
    <section><h2>Твоя подборка</h2>${references.length ? references.map((reference) => `<article class="panel reference"><p>${escapeHtml(reference.notes)}</p>${reference.url ? `<a href="${escapeHtml(/^https?:\/\//.test(reference.url) ? reference.url : "#")}" target="_blank" rel="noopener">Открыть источник ↗</a>` : ""}${reference.asset_id ? `<div class="actions"><button data-analyze="${reference.asset_id}">Разобрать видео</button></div>` : ""}${reference.analysis ? `<details><summary>Разбор</summary><p>${escapeHtml(reference.analysis)}</p></details>` : ""}</article>`).join("") : '<p class="muted">Здесь появятся твои примеры и заметки о подаче.</p>'}</section></div>`;
}

function showSettings() {
  title.textContent = "Подключения";
  content.innerHTML = `<section class="panel"><h2>Сервисы</h2>${[
    [
      "OpenAI",
      connectionStatus.openai,
      "Сценарии, анализ видео и монтажный план",
    ],
    [
      "HeyGen · аватар",
      connectionStatus.heygen,
      "Твой аватар и выбранный голос",
    ],
    [
      "HeyGen · видеовставки",
      connectionStatus.video_generation,
      "Генерация дополнительных кадров",
    ],
    ["Metricool", connectionStatus.metricool, "Планирование публикации Reels"],
  ]
    .map(
      ([name, ready, description]) =>
        `<div class="connection"><div><strong>${name}</strong><div class="muted small">${description}</div></div><span class="status">${ready ? "Настроен" : "Нужна настройка"}</span></div>`,
    )
    .join("")}
    <p class="hint">Ключи хранятся в файле .env на твоём компьютере или сервере. После изменений перезапусти приложение и worker. Наличие ключа ещё не подтверждает доступ к API.</p>
    <p class="muted small">Модель ${escapeHtml(connectionStatus.model)}</p></section>`;
}

async function showView(view) {
  unsavedChanges = false;
  currentView = view;
  document
    .querySelectorAll(".nav")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.view === view),
    );
  connectionStatus = await api("/api/status");
  if (view === "projects") {
    currentProject = null;
    await showProjects();
  }
  if (view === "project") await showProject(currentProject);
  if (view === "references") await showReferences();
  if (view === "jobs") {
    title.textContent = "Задачи";
    content.innerHTML = `<section class="panel">${jobsMarkup(await api("/api/jobs")) || '<p class="muted">Задач пока нет.</p>'}</section>`;
  }
  if (view === "settings") showSettings();
}

document.querySelector("#new-project").onclick = () => dialog.showModal();
document.querySelector("[data-close]").onclick = () => dialog.close();
document.querySelector("#create-form").onsubmit = async (event) => {
  event.preventDefault();
  try {
    const result = await api(
      "/api/projects",
      "POST",
      Object.fromEntries(new FormData(event.target)),
    );
    dialog.close();
    event.target.reset();
    currentProject = result.id;
    await showView("project");
  } catch (error) {
    notice(error.message);
  }
};

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button) return;
  try {
    if (button.dataset.view) await showView(button.dataset.view);
    if (button.hasAttribute("data-create")) dialog.showModal();
    if (button.dataset.project) {
      currentProject = button.dataset.project;
      await showView("project");
    }
    if (button.hasAttribute("data-generate"))
      await action(`/api/projects/${currentProject}/generate`, {
        feedback: document.querySelector("#feedback").value,
        research: document.querySelector("#research").checked,
      });
    if (button.dataset.projectAction) {
      const body =
        button.dataset.projectAction === "edit"
          ? { auto_render: true, feedback: document.querySelector("#edit-feedback")?.value || "" }
          : undefined;
      await action(
        `/api/projects/${currentProject}/${button.dataset.projectAction}`,
        body,
      );
    }
    if (button.dataset.mode)
      await action(`/api/projects/${currentProject}/production`, {
        mode: button.dataset.mode,
        auto_render: button.dataset.mode === "avatar" && Boolean(connectionStatus.openai),
      });
    if (button.hasAttribute("data-save-script"))
      await action(
        `/api/projects/${currentProject}/script`,
        JSON.parse(document.querySelector("#script-json").value),
        "PUT",
      );
    if (button.hasAttribute("data-save-edit"))
      await action(
        `/api/projects/${currentProject}/edit`,
        JSON.parse(document.querySelector("#edit-json").value),
        "PUT",
      );
    if (button.dataset.analyze)
      await action(`/api/assets/${button.dataset.analyze}/analyze`);
    if (button.dataset.jobRetry)
      await action(`/api/jobs/${button.dataset.jobRetry}/retry`);
    if (button.dataset.jobDismiss)
      await action(`/api/jobs/${button.dataset.jobDismiss}/dismiss`);
  } catch (error) {
    notice(error.message);
  }
});

content.addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  const submit = form.querySelector(
    'button[type="submit"], button:not([type])',
  );
  if (submit) submit.disabled = true;
  try {
    const data = new FormData(form);
    if (form.id === "upload-form") {
      data.set("project_id", currentProject);
      await api("/api/assets", "POST", data);
    }
    if (form.id === "reference-form") {
      let assetId = null;
      if (data.get("file").size) {
        const upload = new FormData();
        upload.set("file", data.get("file"));
        upload.set("kind", "reference");
        assetId = (await api("/api/assets", "POST", upload)).id;
      }
      await api("/api/references", "POST", {
        url: data.get("url"),
        notes: data.get("notes"),
        asset_id: assetId,
      });
    }
    if (form.id === "clip-form")
      await api(`/api/projects/${currentProject}/generate-clip`, "POST", {
        prompt: data.get("prompt"),
        duration: Number(data.get("duration")),
      });
    if (form.id === "schedule-form")
      await api(`/api/projects/${currentProject}/schedule`, "POST", {
        public_video_url: data.get("public_video_url"),
        publication_at: data.get("publication_at") + ":00+03:00",
      });
    notice("Сохранено", true);
    await showView(currentView);
  } catch (error) {
    notice(error.message);
  } finally {
    if (submit) submit.disabled = false;
  }
});

// Only refresh a project when a background job changes. Preserve edits and video playback.
let lastJobs = "";
content.addEventListener("input", () => {
  unsavedChanges = true;
});
setInterval(async () => {
  if (
    refreshing ||
    unsavedChanges ||
    !currentProject ||
    document.activeElement.matches("textarea,input,select")
  )
    return;
  const video = content.querySelector("video");
  if (video && !video.paused) return;
  refreshing = true;
  try {
    const project = await api("/api/projects/" + currentProject);
    const signature = JSON.stringify(
      project.jobs.map((job) => [job.id, job.status]),
    );
    if (lastJobs && signature !== lastJobs) await showProject(currentProject);
    lastJobs = signature;
  } catch (error) {
    console.warn("Не удалось обновить статус задачи");
  } finally {
    refreshing = false;
  }
}, 5000);

showView("projects").catch((error) => notice(error.message));
