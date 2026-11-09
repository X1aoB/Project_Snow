// 功能调研问卷（角色语音、模型托管与多端同步）。只在用户点「去填写」时才加载；题目和文案全部来自 /voice-survey.json，
// 改字不用动代码。这里不碰 IndexedDB，答案只提交给服务器，是否已答由 app.js 记在 localStorage。
const STYLE_URL = "/modules/voice-survey.css";
const FIELD_DEFAULTS = Object.freeze({ use_scenes: [], priorities: [], characters: [], hosted_models: [], app_platforms: [], adult: "", comment: "" });

function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
  node.append(...children);
  return node;
}

// 样式加载失败时表单仍然可用，只是朴素一些，所以不把失败往外抛。
function loadStyles() {
  if (document.querySelector(`link[href="${STYLE_URL}"]`)) return Promise.resolve();
  return new Promise((resolve) => {
    const link = el("link", { rel: "stylesheet", href: STYLE_URL });
    link.onload = resolve;
    link.onerror = resolve;
    document.head.append(link);
  });
}

async function loadCopy() {
  const response = await fetch("/voice-survey.json", { cache: "no-cache" });
  if (!response.ok) throw new Error("voice_survey_copy_unavailable");
  const copy = await response.json();
  if (copy?.schema_version !== 1 || !Array.isArray(copy.questions)) throw new Error("voice_survey_copy_invalid");
  return copy;
}

function choice(type, name, value, label) {
  return el("label", { class: "voice-survey-option" }, el("input", { type, name, value }), el("span", {}, label));
}

function questionField(question, characters) {
  const field = el("fieldset", { class: "voice-survey-question", "data-question": question.id });
  field.append(el("legend", {}, question.label));
  if (question.hint) field.append(el("small", { class: "voice-survey-hint" }, question.hint));
  if (question.type === "text") {
    field.append(el("textarea", { name: question.id, maxlength: String(question.maxLength), rows: "3", "aria-label": question.label }));
  } else if (question.type === "scale") {
    const scale = el("div", { class: "voice-survey-options voice-survey-scale" });
    for (let value = question.min; value <= question.max; value += 1) scale.append(choice("radio", question.id, String(value), String(value)));
    field.append(scale, el("div", { class: "voice-survey-scale-ends" }, el("span", {}, question.minLabel), el("span", {}, question.maxLabel)));
  } else if (question.type === "characters") {
    field.append(el("div", { class: "voice-survey-options voice-survey-characters" },
      ...characters.map((character) => choice("checkbox", question.id, character.character_id, character.display_name))));
  } else {
    field.append(el("div", { class: "voice-survey-options" },
      ...question.options.map((option) => choice(question.type === "multi" ? "checkbox" : "radio", question.id, option.value, option.label))));
  }
  return field;
}

function collectAnswers(form, copy) {
  const data = new FormData(form);
  const answers = { survey_version: copy.survey_version, ...structuredClone(FIELD_DEFAULTS) };
  for (const question of copy.questions) {
    if (question.type === "multi" || question.type === "characters") answers[question.id] = data.getAll(question.id);
    else if (question.type === "scale") answers[question.id] = data.has(question.id) ? Number(data.get(question.id)) : null;
    else if (data.has(question.id)) answers[question.id] = String(data.get(question.id)).trim();
  }
  return answers;
}

export async function openVoiceSurvey({ api, characters, onSaved, notify }) {
  const [copy, status] = await Promise.all([loadCopy(), api("/voice-survey", { timeoutMs: 10000 }), loadStyles()]);
  const dialog = el("dialog", { class: "modal voice-survey-modal", "aria-labelledby": "voice-survey-title" });
  const error = el("p", { class: "voice-survey-error", role: "alert", hidden: "" });
  const submit = el("button", { type: "submit", class: "primary-button" }, copy.submit);
  const later = el("button", { type: "button", class: "secondary-button", "data-voice-survey-later": "" }, copy.later);
  const form = el("form", { novalidate: "" },
    el("header", {},
      el("div", {}, el("span", { class: "panel-kicker" }, copy.kicker), el("h2", { id: "voice-survey-title" }, copy.title)),
      el("button", { type: "button", class: "icon-button", "aria-label": copy.close, "data-voice-survey-later": "" }, "×")),
    el("div", { class: "voice-survey-intro" }, ...copy.intro.map((line) => el("p", {}, line)), el("p", { class: "voice-survey-privacy" }, copy.privacy)),
    ...(status?.answered ? [el("p", { class: "voice-survey-note" }, copy.resubmitNote)] : []),
    ...copy.questions.flatMap((question) => [
      ...(question.section ? [el("h3", { class: "voice-survey-section" }, question.section)] : []),
      questionField(question, characters),
    ]),
    error,
    el("div", { class: "modal-actions" }, later, submit));
  dialog.append(form);
  document.body.append(dialog);

  const showError = (message) => { error.textContent = message; error.hidden = false; };
  return new Promise((resolve) => {
    let saved = false;
    dialog.addEventListener("close", () => { dialog.remove(); resolve(saved); });
    dialog.addEventListener("click", (event) => { if (event.target.closest?.("[data-voice-survey-later]")) dialog.close(); });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submit.disabled) return;
      const answers = collectAnswers(form, copy);
      const missing = copy.questions.find((question) => question.required && (answers[question.id] === null || answers[question.id] === undefined || answers[question.id] === ""));
      if (missing) {
        showError(copy.requiredError);
        form.querySelector(`[data-question="${missing.id}"] input`)?.focus();
        return;
      }
      submit.disabled = true;
      error.hidden = true;
      try {
        await api("/voice-survey", { method: "POST", timeoutMs: 15000, body: JSON.stringify(answers) });
        saved = true;
        onSaved?.();
        notify?.(copy.saved);
        dialog.close();
      } catch {
        showError(copy.failedError);
      } finally {
        submit.disabled = false;
      }
    });
    dialog.showModal();
  });
}
