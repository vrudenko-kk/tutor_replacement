"use strict";

const byId = (id) => document.getElementById(id);
const nodes = {
  calculate: byId("calculate"), example: byId("example"), upload: byId("upload"),
  download: byId("download"), metadata: byId("request-meta"), error: byId("source-error"),
  summary: byId("summary"), assignments: byId("assignments"), stage: byId("stage"),
  rank: byId("rank-panel"), audit: byId("audit-panel"),
};
const stages = new Map([
  ["BASE", "Базовый подбор"], ["GENERAL", "Проверка общего уровня"],
  ["ALL_RELATED", "Расширение смежности"], ["YELLOW", "Жёлтый светофор"],
  ["OUTER_GAP", "Длинное окно"], ["CROSS_CAMPUS", "Переезд между площадками"],
  ["ONLINE", "Онлайн-замена"], ["RAISE_CAP", "Повышенный лимит"],
]);
const windows = new Map([
  ["inner_gap", "окно между занятиями"], ["adjacent", "соседняя пара"],
  ["full_day", "свободный день"], ["outer_gap", "длинное окно"],
  ["cross_campus", "переезд между площадками"],
]);
const reasons = new Map([
  ["initiator", "инициатор заявки"], ["department", "другая кафедра или нет действующей должности"],
  ["daily_cap", "достигнут дневной лимит"], ["competence_refused", "отказ по дисциплине"],
  ["disciplinary_competence", "нет компетенции по дисциплине"],
  ["related_competence", "нет смежной компетенции"],
  ["traffic_red", "красный светофор"], ["yellow_requires_escalation", "жёлтый светофор пока недоступен"],
  ["campus_refused", "площадка исключена преподавателем"], ["history_zero", "действует запрет из истории"],
  ["time_conflict", "преподаватель занят в это время"],
  ["outer_gap_requires_escalation", "длинное окно пока недоступно"],
  ["cross_campus_requires_escalation", "межплощадочный переезд пока недоступен"],
  ["travel_time_missing", "время переезда неизвестно"], ["travel_time_insufficient", "не хватает времени на переезд"],
  ["declined_or_timed_out", "отказ или истёкший таймаут"],
]);
const number = new Intl.NumberFormat("ru-RU");
let scenario;
let result;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function teacherName(id) {
  return scenario.data.teachers[id]?.name || id;
}
function lessonName(id) {
  const lesson = scenario.data.lessons[id];
  return scenario.data.disciplines[lesson.discipline]?.name || lesson.discipline;
}
function stageName(stage) {
  return stages.get(stage) || stage;
}
function showError(message) {
  nodes.error.textContent = message;
  nodes.error.hidden = !message;
}
function setWorking(busy) {
  nodes.calculate.disabled = busy || !scenario;
  nodes.example.disabled = busy;
  nodes.upload.disabled = busy;
  nodes.download.disabled = busy || !result;
  nodes.assignments.setAttribute("aria-busy", String(busy));
}
function renderMetadata(name) {
  const { request } = scenario;
  const values = [
    ["Сценарий", name],
    ["Номер", request.id],
    ["Дата замены", request.day.split("-").reverse().join(".")],
    ["Инициатор", teacherName(request.initiator)],
    ["Согласование", request.mode === "parallel" ? "Параллельное" : "Последовательное"],
  ];
  nodes.metadata.replaceChildren(...values.map(([label, value]) => {
    const item = element("div");
    item.append(element("dt", label), element("dd", value));
    return item;
  }));
}

function renderAssignments() {
  const lessonIds = scenario.request.lesson_ids;
  const count = Object.keys(result.assignments).length;
  nodes.stage.textContent = stageName(result.stage);
  nodes.stage.hidden = false;
  nodes.summary.className = result.complete ? "summary success" : "summary incomplete";
  nodes.summary.textContent = result.complete
    ? `Замены подобраны для всех пар (${count} из ${lessonIds.length}).`
    : `Подобрано пар: ${count} из ${lessonIds.length}. Остальные требуют решения секретаря.`;
  nodes.assignments.replaceChildren(...lessonIds.map((id) => {
    const lesson = scenario.data.lessons[id];
    const slot = scenario.data.slots[lesson.slot];
    const assignment = result.assignments[id];
    const description = element("div");
    description.append(element("strong", lessonName(id)), element("small", `${slot.start}–${slot.end} · ${lesson.group}`));
    const substitute = element("div");
    if (assignment) {
      const where = assignment.online ? `Онлайн · площадка ${assignment.campus}` : `Площадка ${assignment.campus}`;
      substitute.append(element("strong", teacherName(assignment.teacher)), element("small", where));
    } else {
      substitute.append(element("strong", "Замена не подобрана", "unassigned"));
    }
    const row = element("li");
    row.append(description, substitute);
    return row;
  }));
}

function renderRank() {
  nodes.rank.replaceChildren(...scenario.request.lesson_ids.map((id, index) => {
    const section = element("details");
    section.open = index === 0;
    section.append(element("summary", lessonName(id)));
    const entries = result.ranked[id];
    if (entries.length === 0) {
      section.append(element("p", "Допустимых кандидатов нет.", "muted"));
    } else {
      const list = element("ul");
      for (const candidate of entries) {
        const window = windows.get(candidate.window) || candidate.window;
        const coverage = candidate.allows_full_coverage ? "все пары" : `${candidate.global_coverage} из ${scenario.request.lesson_ids.length} пар`;
        list.append(element("li", `${teacherName(candidate.teacher)} · ${window} · стоимость ${number.format(candidate.cost)} · покрытие: ${coverage}`));
      }
      section.append(list);
    }
    return section;
  }));
}

function renderAudit() {
  nodes.audit.replaceChildren(...result.audit.map((step, index) => {
    const section = element("details");
    section.open = index === result.audit.length - 1;
    section.append(element("summary", `${stageName(step.stage)} · ${step.covered} из ${step.required} пар`));
    for (const [lesson, rejected] of Object.entries(step.rejected)) {
      section.append(element("h3", lessonName(lesson)));
      const entries = Object.entries(rejected);
      if (entries.length === 0) {
        section.append(element("p", "Все кандидаты прошли проверку.", "muted"));
      } else {
        const list = element("ul");
        for (const [teacher, rejectReasons] of entries) {
          const message = rejectReasons.map((reason) => reasons.get(reason) || reason).join(", ");
          list.append(element("li", `${teacherName(teacher)} — ${message}`));
        }
        section.append(list);
      }
    }
    return section;
  }));
}

async function fetchJson(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Не удалось выполнить запрос.");
  return data;
}

async function run(loadScenario, displayName, isExample) {
  setWorking(true);
  showError("");
  const previousMessage = nodes.summary.textContent;
  const previousClass = nodes.summary.className;
  nodes.summary.className = "summary";
  nodes.summary.textContent = "Выполняется расчёт…";
  try {
    const next = await loadScenario();
    const plan = await fetchJson("/api/plan", {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(next),
    });
    scenario = next;
    result = plan;
    renderMetadata(displayName);
    renderAssignments();
    renderRank();
    renderAudit();
    nodes.example.hidden = isExample;
  } catch (error) {
    nodes.summary.className = previousClass;
    nodes.summary.textContent = result ? previousMessage : "Расчёт пока недоступен.";
    showError(error.message || String(error));
    if (!scenario) nodes.example.hidden = false;
  } finally {
    setWorking(false);
  }
}

const loadExample = () => run(() => fetchJson("/api/example"), "Учебный пример", true);
nodes.example.addEventListener("click", loadExample);
nodes.calculate.addEventListener("click", () => {
  const isExample = nodes.example.hidden;
  const name = nodes.metadata.querySelector("dd").textContent;
  run(() => scenario, name, isExample);
});
nodes.upload.addEventListener("change", () => {
  const file = nodes.upload.files[0];
  if (!file) return;
  nodes.upload.value = "";
  run(async () => {
    if (file.size > 2_000_000) throw new Error("Размер файла превышает 2 МБ.");
    try {
      return JSON.parse(await file.text());
    } catch {
      throw new Error("Файл должен содержать корректный JSON.");
    }
  }, file.name, false);
});
nodes.download.addEventListener("click", () => {
  if (!result) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], {type: "application/json"}));
  const link = element("a");
  link.href = url;
  link.download = "substitution-result.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});

const tabs = Array.from(document.querySelectorAll('[role="tab"]'));
function selectTab(selected) {
  for (const tab of tabs) {
    const isSelected = tab === selected;
    tab.setAttribute("aria-selected", String(isSelected));
    tab.tabIndex = isSelected ? 0 : -1;
    byId(tab.getAttribute("aria-controls")).hidden = !isSelected;
  }
}
for (const [index, tab] of tabs.entries()) {
  tab.addEventListener("click", () => selectTab(tab));
  tab.addEventListener("keydown", (event) => {
    const indices = {
      ArrowLeft: (index + tabs.length - 1) % tabs.length,
      ArrowRight: (index + 1) % tabs.length,
      Home: 0,
      End: tabs.length - 1,
    };
    if (!Object.hasOwn(indices, event.key)) return;
    event.preventDefault();
    const selected = tabs[indices[event.key]];
    selectTab(selected);
    selected.focus();
  });
}
for (const link of document.querySelectorAll("nav a")) {
  link.addEventListener("click", () => {
    document.querySelector("nav a[aria-current]")?.removeAttribute("aria-current");
    link.setAttribute("aria-current", "location");
  });
}
loadExample();
