"use strict";

(function () {
  var I18N = {
    ru: {
      title: "Ассистент", newChat: "Новый диалог", settings: "Настройки", close: "Закрыть",
      emptyTitle: "Опишите задачу печати", emptyHint: "Приложите модель STL, OBJ, 3MF, ТЗ DOCX или TXT, фото",
      placeholder: "Например: кронштейн под нагрузку, PETG, без поддержек", attach: "Прикрепить файл",
      answerPlaceholder: "Или напишите свой вариант…",
      send: "Отправить", stop: "Остановить", drop: "Отпустите файлы",
      baseUrl: "URL API", apiKey: "Ключ API", model: "Модель", outputDir: "Папка для G-code и проектов",
      workerPython: "Python ядра (путь к python.exe)",
      planningInputs: "JSON измерений, нагрузок и шума (необязательно)",
      language: "Язык", theme: "Тема", light: "Светлая", dark: "Тёмная", save: "Сохранить", test: "Проверить",
      saved: "Сохранено", testing: "Проверка…", testOk: "Связь с моделью есть", keyKept: "Ключ сохранён",
      thinking: "Думает", thought: "Ход мыслей", offline: "нет связи со слайсером",
      notConfigured: "Укажите URL, ключ и модель в настройках.", openSettings: "Открыть настройки",
      llmError: "Ошибка модели: ", internalError: "Ошибка: ", tooManySteps: "Слишком много шагов, задача остановлена.",
      stopped: "Остановлено", busy: "Ассистент ещё работает",
      done: "Готово", failed: "Ошибка", retried: "исправлено повтором",
      askHint: "Выберите вариант или напишите свой ответ ниже", askCancelled: "Вопрос отменён",
      upEmpty: "файл пустой", upLarge: "больше 300 МБ", upUnreadable: "не удалось прочитать файл",
      upNetwork: "нет связи со слайсером", upUnsupported: "формат не поддерживается: STL, OBJ, 3MF, JPG, PNG, DOCX, TXT",
      upBroken: "не удалось прочитать текст ТЗ", spec: "ТЗ",
      h: "ч", min: "мин", s: "с", g: "г", m: "м",
      preset: "Пресет STE", presetMissing: "Пресет не подобран", overPreset: "Поверх пресета",
      noChanges: "Изменения поверх пресета не нужны", options: "Варианты", alternatives: "Другие пресеты",
      st_candidate: "предложение", st_needs_input: "нужны данные", st_needs_calibration: "нужна калибровка",
      st_infeasible: "невыполнимо", st_search_incomplete: "поиск не завершён", st_slice_verified: "проверено слайсингом",
      j_requirement: "по ТЗ", j_explicit: "обязательно", j_constraint: "ограничение", j_structural: "силовая деталь",
      j_elastic: "эластичная деталь", j_decorative: "декоративная деталь", legacy: "без пресета (профиль планировщика)"
    },
    en: {
      title: "Assistant", newChat: "New chat", settings: "Settings", close: "Close",
      emptyTitle: "Describe the print job", emptyHint: "Attach an STL, OBJ, 3MF model, a DOCX or TXT specification, a photo",
      placeholder: "E.g. load-bearing bracket, PETG, no supports", attach: "Attach file",
      answerPlaceholder: "Or type your own answer…",
      send: "Send", stop: "Stop", drop: "Drop files here",
      baseUrl: "API URL", apiKey: "API key", model: "Model", outputDir: "Folder for G-code and projects",
      workerPython: "Core Python (path to python.exe)",
      planningInputs: "Measurements, loads and noise JSON (optional)",
      language: "Language", theme: "Theme", light: "Light", dark: "Dark", save: "Save", test: "Test",
      saved: "Saved", testing: "Testing…", testOk: "Model is reachable", keyKept: "Key is stored",
      thinking: "Thinking", thought: "Reasoning", offline: "slicer is not reachable",
      notConfigured: "Set the URL, key and model in settings.", openSettings: "Open settings",
      llmError: "Model error: ", internalError: "Error: ", tooManySteps: "Too many steps, the task was stopped.",
      stopped: "Stopped", busy: "The assistant is still working",
      done: "Done", failed: "Failed", retried: "fixed by a retry",
      askHint: "Pick an option or type your own answer below", askCancelled: "Question cancelled",
      upEmpty: "the file is empty", upLarge: "larger than 300 MB", upUnreadable: "could not read the file",
      upNetwork: "slicer is not reachable", upUnsupported: "unsupported format: STL, OBJ, 3MF, JPG, PNG, DOCX, TXT",
      upBroken: "could not read the specification text", spec: "Spec",
      h: "h", min: "min", s: "s", g: "g", m: "m",
      preset: "STE preset", presetMissing: "No preset selected", overPreset: "On top of the preset",
      noChanges: "No changes over the preset needed", options: "Options", alternatives: "Other presets",
      st_candidate: "proposal", st_needs_input: "needs input", st_needs_calibration: "needs calibration",
      st_infeasible: "infeasible", st_search_incomplete: "search incomplete", st_slice_verified: "slice verified",
      j_requirement: "from the spec", j_explicit: "required", j_constraint: "constraint", j_structural: "load-bearing part",
      j_elastic: "elastic part", j_decorative: "decorative part", legacy: "no preset (planner profile)"
    }
  };

  // Tool titles and the step group they belong to.
  var TOOLS = {
    list_model_sources: ["Исходные файлы моделей", "Model sources", "read"],
    plan_print: ["Расчёт ядром по ТЗ", "Core calculation", "plan"],
    get_plan: ["План и ограничения", "Plan and limitations", "read"],
    apply_plan: ["Применение плана", "Apply plan", "apply"],
    apply_plan_to_project: ["План в текущий проект", "Plan into the open project", "apply"],
    revert_plan_changes: ["Откат плана", "Revert plan", "apply"],
    verify_plan: ["Проверка настроек STE", "STE readback", "apply"],
    get_state: ["Состояние слайсера", "Slicer state", "read"],
    find_settings: ["Поиск настроек", "Find settings", "read"],
    get_settings: ["Чтение настроек", "Read settings", "read"],
    set_settings: ["Изменение настроек", "Change settings", "settings"],
    set_print_mode: ["Режим печати", "Printing mode", "settings"],
    list_profiles: ["Список профилей", "List profiles", "read"],
    select_profile: ["Выбор профиля", "Select profile", "settings"],
    create_profile: ["Создание профиля", "Create profile", "settings"],
    save_profile: ["Сохранение профиля", "Save profile", "settings"],
    list_materials: ["Список материалов", "List materials", "read"],
    set_nozzle: ["Сопло экструдера", "Extruder nozzle", "settings"],
    set_material: ["Выбор материала", "Select material", "settings"],
    load_model: ["Загрузка модели", "Load model", "scene"],
    list_objects: ["Объекты сцены", "Scene objects", "read"],
    move_object: ["Перемещение", "Move", "scene"],
    rotate_object: ["Поворот", "Rotate", "scene"],
    scale_object: ["Масштаб", "Scale", "scene"],
    arrange_all: ["Авторасстановка", "Arrange all", "scene"],
    delete_object: ["Удаление", "Delete", "scene"],
    slice: ["Слайсинг", "Slice", "slice"],
    get_slice_status: ["Статус слайсинга", "Slice status", "read"],
    get_slice_result: ["Результат слайсинга", "Slice result", "read"],
    export_gcode: ["Экспорт G-code", "Export G-code", "files"],
    save_project: ["Сохранение проекта 3MF", "Save 3MF project", "files"]
  };
  // Step titles when the model gave no caption: [running, done] per language.
  var STEPS = {
    read: {ru: ["Изучаю проект", "Изучил проект"], en: ["Checking the project", "Checked the project"]},
    plan: {ru: ["Рассчитываю план ядром", "Рассчитал план ядром"], en: ["Running the core calculation", "Ran the core calculation"]},
    apply: {ru: ["Применяю план", "Применил план"], en: ["Applying the plan", "Applied the plan"]},
    settings: {ru: ["Меняю настройки", "Изменил настройки"], en: ["Changing settings", "Changed settings"]},
    scene: {ru: ["Работаю со сценой", "Изменил сцену"], en: ["Arranging the scene", "Changed the scene"]},
    slice: {ru: ["Нарезаю модель", "Нарезал модель"], en: ["Slicing", "Sliced"]},
    files: {ru: ["Сохраняю файлы", "Сохранил файлы"], en: ["Saving files", "Saved files"]}
  };
  // Names of common settings when the slicer did not provide a label (e.g. MCP clients, older plans).
  var LABELS = {
    printing_mode: ["Режим печати", "Printing mode"], layer_height: ["Высота слоя", "Layer height"],
    line_width: ["Ширина линии", "Line width"], wall_line_count: ["Количество стенок", "Wall line count"],
    top_layers: ["Слоёв сверху", "Top layers"], bottom_layers: ["Слоёв снизу", "Bottom layers"],
    top_bottom_thickness: ["Толщина верха и низа", "Top/bottom thickness"],
    infill_sparse_density: ["Плотность заполнения", "Infill density"], infill_pattern: ["Шаблон заполнения", "Infill pattern"],
    material_print_temperature: ["Температура сопла", "Printing temperature"],
    material_bed_temperature: ["Температура стола", "Build plate temperature"], speed_print: ["Скорость печати", "Print speed"],
    cool_fan_speed: ["Обдув", "Fan speed"], support_enable: ["Поддержки", "Supports"], adhesion_type: ["Прилипание", "Adhesion"],
    machine_nozzle_size: ["Диаметр сопла", "Nozzle size"],
    cylindrical_mode_base_diameter: ["Диаметр базы 5D", "5D base diameter"],
    non_printing_base_diameter: ["Диаметр непечатной базы", "Non-printing base diameter"]
  };
  var MODES = {classic: "3D", cylindrical: "5D Spiral", cylindrical_full: "5D"};
  var CATEGORY_ORDER = ["plan", "apply", "settings", "scene", "slice", "files", "read"];
  var CAPTION_MAX = 280;

  var token = (location.hash.match(/t=([0-9a-f]+)/) || [])[1] || "";
  var lang = "ru";
  var seq = 0;
  var busy = false;
  var settings = {};
  var events = {};      // seq -> event (patches folded in), each with a _rev counter
  var order = [];       // event seqs in arrival order
  var cache = {};       // item key -> {sig, node}
  var openDetails = {}; // item key -> bool
  var openCalls = {};   // tool seq -> bool
  var pending = [];
  var question = null;  // the pending question event
  var $ = function (id) { return document.getElementById(id); };
  var feed = $("feed");

  function t(key) { return (I18N[lang] || I18N.ru)[key] || key; }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function api(path, options) {
    options = options || {};
    options.headers = Object.assign({"X-Token": token}, options.headers || {});
    if (options.json !== undefined) {
      options.body = JSON.stringify(options.json);
      options.headers["Content-Type"] = "application/json";
      delete options.json;
    }
    return fetch(path, options).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) { var e = new Error(data.error || r.status); e.status = r.status; e.code = data.error; throw e; }
        return data;
      });
    });
  }

  // ---------------------------------------------------------------- static texts, theme

  function applyTexts() {
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-i18n]").forEach(function (n) { n.textContent = t(n.dataset.i18n); });
    document.querySelectorAll("[data-i18n-title]").forEach(function (n) { n.title = t(n.dataset.i18nTitle); });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(function (n) { n.placeholder = t(n.dataset.i18nPlaceholder); });
    updateComposer();
  }

  function applySettings(s) {
    settings = s || settings;
    lang = settings.language === "en" ? "en" : "ru";
    document.documentElement.dataset.theme = settings.theme === "dark" ? "dark" : "light";
    applyTexts();
  }

  // ---------------------------------------------------------------- formatting

  function fmt(value) {
    if (value === null || value === undefined) return "—";
    if (value === true) return lang === "ru" ? "да" : "on";
    if (value === false) return lang === "ru" ? "нет" : "off";
    if (typeof value === "number") return String(Math.round(value * 1000) / 1000);
    if (typeof value === "object") return JSON.stringify(value);
    return String(value);
  }

  function duration(seconds) {
    seconds = Math.round(seconds || 0);
    var h = Math.floor(seconds / 3600), m = Math.floor(seconds % 3600 / 60);
    if (h) return h + " " + t("h") + (m ? " " + m + " " + t("min") : "");
    if (m) return m + " " + t("min");
    return seconds + " " + t("s");
  }

  function inline(text) {
    var safe = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return safe
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  }

  function markdown(text) {
    var out = [], list = null;
    function closeList() { if (list) { out.push("</" + list + ">"); list = null; } }
    text.split(/\n/).forEach(function (line) {
      var bullet = line.match(/^\s*[-*•]\s+(.*)$/), number = line.match(/^\s*\d+[.)]\s+(.*)$/);
      var heading = line.match(/^\s*#{1,6}\s+(.*)$/);
      if (bullet || number) {
        var kind = bullet ? "ul" : "ol";
        if (list !== kind) { closeList(); out.push("<" + kind + ">"); list = kind; }
        out.push("<li>" + inline((bullet || number)[1]) + "</li>");
      } else if (!line.trim()) {
        closeList();
      } else {
        closeList();
        out.push("<p>" + (heading ? "<strong>" + inline(heading[1]) + "</strong>" : inline(line)) + "</p>");
      }
    });
    closeList();
    return out.join("");
  }

  function base(path) { return String(path || "").split(/[\\/]/).pop(); }

  function toolTitle(name) {
    var titles = TOOLS[name];
    return titles ? titles[lang === "ru" ? 0 : 1] : name;
  }

  function toolArg(e) {
    var a = e.args || {};
    if (typeof a !== "object") return "";
    switch (e.name) {
      case "set_settings": return (a.changes || []).map(function (c) { return c.key; }).join(", ");
      case "get_settings": return (a.keys || []).join(", ");
      case "find_settings": return (a.queries || []).concat(a.query ? [a.query] : []).join(", ");
      case "set_print_mode": return {classic: "3D", cylindrical: "5D Spiral", cylindrical_full: "5D"}[a.mode] || a.mode || "";
      case "select_profile": return a.custom_name || a.quality_type || "";
      case "create_profile": return a.name || "";
      case "list_materials": return a.query || "";
      case "set_material": return a.material_id || "";
      case "load_model": return base(a.path);
      case "move_object": return "#" + a.id + " " + ["x", "y", "z"].filter(function (k) { return a[k] !== undefined; })
        .map(function (k) { return k.toUpperCase() + " " + a[k]; }).join(" ");
      case "rotate_object": return "#" + a.id + " " + String(a.axis || "").toUpperCase() + " " + a.angle + "°";
      case "scale_object": return "#" + a.id + " " + (a.percent !== undefined ? a.percent + "%" :
        a.size_mm !== undefined ? (a.axis || "z").toUpperCase() + " " + a.size_mm + " mm" : "");
      case "delete_object": return a.all ? "*" : "#" + a.id;
      default: return "";
    }
  }

  // ---------------------------------------------------------------- grouping

  // Phase of a tool for grouping: reads join any step; a different phase starts a new step.
  var PHASES = {settings: "work", scene: "work", plan: "plan", apply: "plan", slice: "output", files: "output"};
  function phase(e) { return PHASES[(TOOLS[e.name] || [])[2]] || null; }

  // Turn the event stream into feed items. Consecutive tool calls form one step: a new step starts only with
  // a new caption (the short sentence the model writes with its calls) or a new phase of work. Reasoning between
  // rounds is folded into the step; reasoning before a final answer stays a separate collapsed block.
  function buildItems() {
    var items = [], group = null, thoughts = [];

    function flushThoughts() {
      if (thoughts.length) items.push({kind: "thinking", key: "e" + thoughts[0].seq, members: thoughts});
      thoughts = [];
    }
    function open(caption, first) {
      group = {kind: "step", key: "g" + (caption || thoughts[0] || first).seq, caption: caption, auto: !!(caption && caption.auto),
               tools: [], thoughts: thoughts, members: thoughts.slice(), phase: null};
      thoughts = [];
      if (caption) group.members.push(caption);
      items.push(group);
    }
    function absorbThoughts() {
      if (!thoughts.length) return;
      group.thoughts = group.thoughts.concat(thoughts);
      group.members = group.members.concat(thoughts);
      thoughts = [];
    }

    order.forEach(function (s) {
      var e = events[s];
      if (!e) return;
      if (e.type === "thinking") {
        thoughts.push(e);
      } else if (e.type === "message" && e.note) {
        if ((e.text || "").length > CAPTION_MAX) { thoughts.push(e); return; }
        if (!group || group.tools.length || group.caption) { open(e, e); } else { group.caption = e; group.members.push(e); }
      } else if (e.type === "tool") {
        if (e.name === "ask_user") return;  // shown as the question card
        var p = phase(e);
        var contextCall = String(e.call_id || "").indexOf("context_") === 0;
        if (!group || (group.auto && !contextCall) || (p && group.phase && p !== group.phase)) open(null, thoughts[0] || e);
        absorbThoughts();
        if (p && !group.phase) group.phase = p;
        group.tools.push(e);
        group.members.push(e);
      } else {
        flushThoughts();
        group = null;
        if (e.type === "user" || e.type === "message" || e.type === "question" || e.type === "error" ||
            (e.type === "done" && e.status === "cancelled")) {
          items.push({kind: e.type, key: "e" + e.seq, members: [e]});
        }
      }
    });
    flushThoughts();
    return items;
  }

  function signature(item, last) {
    return item.members.map(function (e) { return e.seq + ":" + (e._rev || 0); }).join(",") +
      "|" + (openDetails[item.key] ? 1 : 0) + "|" + (last && busy ? 1 : 0) + "|" + lang +
      "|" + item.members.map(function (e) { return openCalls[e.seq] ? e.seq : ""; }).join("");
  }

  function render() {
    var stick = nearBottom();
    var items = buildItems();
    var keep = {};
    var children = [];
    items.forEach(function (item, index) {
      var last = index === items.length - 1;
      var sig = signature(item, last);
      var cached = cache[item.key];
      if (!cached || cached.sig !== sig) {
        var node = renderItem(item, last, cached ? cached.node : null);
        cached = cache[item.key] = {sig: sig, node: node};
      }
      keep[item.key] = true;
      children.push(cached.node);
    });
    Object.keys(cache).forEach(function (k) { if (!keep[k]) delete cache[k]; });
    var empty = $("empty");
    var current = Array.prototype.slice.call(feed.children).filter(function (n) { return n !== empty; });
    var same = current.length === children.length && current.every(function (n, i) { return n === children[i]; });
    if (!same) {
      current.forEach(function (n) { if (children.indexOf(n) < 0) n.remove(); });
      children.forEach(function (n) { feed.appendChild(n); });
    }
    empty.hidden = items.length > 0;
    question = null;
    order.forEach(function (s) { var e = events[s]; if (e && e.type === "question" && e.status === "pending") question = e; });
    updateComposer();
    if (stick) feed.scrollTop = feed.scrollHeight;
  }

  var scheduled = false;
  function scheduleRender() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(function () { scheduled = false; render(); });
  }

  // ---------------------------------------------------------------- items

  function renderItem(item, last, old) {
    var e = item.members[0];
    switch (item.kind) {
      case "user": return renderUser(e);
      case "message": return renderMessage(e);
      case "thinking": return renderThinking(item.members, old);
      case "step": return renderStep(item, last, old);
      case "question": return renderQuestion(e);
      case "error": return renderError(e);
      case "done": return el("div", "stopped", t("stopped"));
    }
    return el("div");
  }

  function renderUser(e) {
    var node = el("div", "msg user", e.text || "");
    if (e.attachments && e.attachments.length) {
      var files = el("div", "files");
      e.attachments.forEach(function (a) {
        var icon = a.kind === "image" ? "▣ " : a.kind === "spec" ? "≡ " : "◇ ";
        files.appendChild(el("span", "", icon + a.name));
      });
      node.appendChild(files);
    }
    return node;
  }

  function renderMessage(e) {
    var node = el("div", "msg ai" + (e.streaming ? " streaming" : ""));
    node.innerHTML = markdown(e.text || "");
    return node;
  }

  function thoughtsBlock(members, wasOpen) {
    var node = el("details", "think");
    var streaming = members.some(function (m) { return m.streaming; });
    node.classList.toggle("streaming", streaming);
    node.open = !!wasOpen;
    node.appendChild(el("summary", "", streaming ? t("thinking") : t("thought")));
    node.appendChild(el("div", "think-body", members.map(function (m) { return (m.text || "").trim(); })
      .filter(Boolean).join("\n\n")));
    return node;
  }

  function renderThinking(members, old) {
    return thoughtsBlock(members, old && old.open);
  }

  function stepStatus(item, last) {
    if (item.tools.some(function (e) { return (e.status || "running") === "running"; })) return "running";
    if (unrecovered(item).length) return "error";
    if (last && busy) return "running";
    return "ok";
  }

  function unrecovered(item) {
    return item.tools.filter(function (e, i) {
      if (e.status !== "error") return false;
      return !item.tools.slice(i + 1).some(function (later) { return later.name === e.name && later.status === "ok"; });
    });
  }

  function stepTitle(item, status) {
    if (item.caption && item.caption.text) return item.caption.text.trim().replace(/[:.]+$/, "");
    var cats = [];
    item.tools.forEach(function (e) {
      var cat = (TOOLS[e.name] || [])[2] || "read";
      if (cats.indexOf(cat) < 0) cats.push(cat);
    });
    cats.sort(function (a, b) { return CATEGORY_ORDER.indexOf(a) - CATEGORY_ORDER.indexOf(b); });
    var useful = cats.filter(function (c) { return c !== "read"; });
    var shown = (useful.length ? useful : cats).slice(0, 2);
    var running = status === "running" ? 0 : 1;
    var parts = shown.map(function (c) { return STEPS[c][lang][running]; });
    if (parts.length > 1) parts[1] = parts[1].charAt(0).toLowerCase() + parts[1].slice(1);
    return parts.join(", ") || (lang === "ru" ? "Работаю" : "Working");
  }

  function stepMeta(item) {
    var counts = [], index = {};
    item.tools.forEach(function (e) {
      if (index[e.name] === undefined) { index[e.name] = counts.length; counts.push([e.name, 0]); }
      counts[index[e.name]][1]++;
    });
    return counts.map(function (c) { return toolTitle(c[0]) + (c[1] > 1 ? " ×" + c[1] : ""); }).join(" · ");
  }

  function mergedChanges(item) {
    var list = [], byKey = {};
    item.tools.forEach(function (e) {
      if (e.status !== "ok") return;
      ((e.result || {}).changes || []).forEach(function (c) {
        var k = c.key + "|" + (c.scope || "");
        if (byKey[k] && c.key !== "rotation" && c.key !== "object") {
          var merged = byKey[k];
          merged.new = c.new; merged.new_text = c.new_text;
        } else {
          byKey[k] = Object.assign({}, c);
          list.push(byKey[k]);
        }
      });
    });
    return list.filter(function (c) {
      return !(c.old !== null && c.old !== undefined && fmt(c.old) === fmt(c.new) && c.old_text === c.new_text);
    });
  }

  function renderStep(item, last, old) {
    var status = stepStatus(item, last);
    var node = el("div", "step " + status);
    var row = el("div", "step-row");
    row.appendChild(el("span", "dot"));
    row.appendChild(el("span", "step-title", stepTitle(item, status)));
    var more = el("button", "more" + (openDetails[item.key] ? " on" : ""), "⋯");
    more.title = lang === "ru" ? "Подробнее" : "Details";
    more.addEventListener("click", function () { openDetails[item.key] = !openDetails[item.key]; scheduleRender(); });
    row.appendChild(more);
    node.appendChild(row);
    if (item.tools.length) node.appendChild(el("div", "step-meta", stepMeta(item)));

    var changes = mergedChanges(item);
    if (changes.length) {
      var box = el("div", "changes" + (old && !old.querySelector(".changes") ? " fresh" : ""));
      changes.forEach(function (c) { box.appendChild(renderChange(c)); });
      node.appendChild(box);
    }

    item.tools.forEach(function (e) {
      if ((e.name === "plan_print" || e.name === "get_plan") && e.status === "ok") node.appendChild(renderPreset(e.result || {}));
    });

    item.tools.forEach(function (e) {
      var r = e.result || {};
      if (e.name === "slice" && (e.status || "running") === "running") {
        var wrap = el("div", "bar-wrap");
        var track = el("div", "track"), fill = el("div", "fill");
        fill.style.width = Math.round((e.progress || 0) * 100) + "%";
        track.appendChild(fill);
        wrap.appendChild(track);
        wrap.appendChild(el("span", "", Math.round((e.progress || 0) * 100) + "%"));
        node.appendChild(wrap);
      }
      if (e.status !== "ok") return;
      if (e.name === "slice") {
        var parts = [r.print_time_s ? duration(r.print_time_s) : r.print_time,
          r.material_g ? r.material_g + " " + t("g") : "", r.material_m ? r.material_m + " " + t("m") : ""].filter(Boolean);
        node.appendChild(el("div", "summary", parts.join(" · ") || t("done")));
      }
      if (r.path && (e.name === "export_gcode" || e.name === "save_project")) node.appendChild(el("div", "note path", r.path));
      (r.warnings || []).forEach(function (w) { node.appendChild(el("div", "note", "! " + w)); });
    });
    unrecovered(item).forEach(function (e) {
      node.appendChild(el("div", "note error", "✕ " + toolTitle(e.name) + ": " + ((e.result || {}).error || t("failed"))));
    });

    if (openDetails[item.key]) node.appendChild(renderDetails(item));
    return node;
  }

  function justification(j) {
    if (!j) return "";
    if (j.indexOf("load_class:") === 0) return t("j_" + j.slice(11)) !== "j_" + j.slice(11) ? t("j_" + j.slice(11)) : j.slice(11);
    return t("j_" + j) !== "j_" + j ? t("j_" + j) : j;
  }

  // Core plan: which STE preset is the base and what changes on top of it (a proposal until applied).
  function renderPreset(r) {
    var box = el("div", "preset");
    var base = r.base_preset || {};
    var head = el("div", "preset-head");
    head.appendChild(el("span", "preset-tag", t("preset")));
    if (base.profile) {
      head.appendChild(el("span", "preset-name", base.profile));
    } else {
      head.appendChild(el("span", "preset-name", base.native_preset === false && base.status === "not_requested" ?
        t("legacy") : t("presetMissing")));
    }
    var sub = [];
    if (r.chosen_material) sub.push(r.chosen_material.label || r.chosen_material.id);
    if (r.route && r.route.mode) sub.push({classic: "3D", cylindrical: "5D Spiral", cylindrical_full: "5D"}[r.route.mode] || r.route.mode);
    if (sub.length) head.appendChild(el("span", "preset-sub", sub.join(" · ")));
    box.appendChild(head);
    if (r.status) box.appendChild(el("div", "preset-status", t("st_" + r.status) !== "st_" + r.status ? t("st_" + r.status) : r.status));
    if (!base.profile && base.reason) {
      box.appendChild(el("div", "note", base.reason));
      if (base.options && base.options.length) {
        var ul = el("ul");
        base.options.forEach(function (o) { ul.appendChild(el("li", "", o)); });
        box.appendChild(ul);
      }
    }
    var changes = (r.setting_changes || []).filter(function (c) {
      return !(c.justification === "explicit" && fmt(c.base_value) === fmt(c.effective_value));
    });
    if (changes.length) {
      var list = el("div", "changes");
      changes.forEach(function (c) {
        var mode = c.key === "printing_mode";
        var row = renderChange({key: c.key, label: c.label, label_local: c.label_local, old: c.base_value,
          new: c.effective_value, old_text: mode ? MODES[c.base_value] : null, new_text: mode ? MODES[c.effective_value] : null,
          unit: c.unit || ""});
        var why = justification(c.justification);
        if (why) row.appendChild(el("span", "why", why));
        list.appendChild(row);
      });
      box.appendChild(list);
    } else if (base.profile) {
      box.appendChild(el("div", "note", t("noChanges")));
    }
    return box;
  }

  function renderDetails(item) {
    var box = el("div", "details");
    if (item.thoughts.length) box.appendChild(thoughtsBlock(item.thoughts, false));
    item.tools.forEach(function (e, i) {
      var status = e.status || "running";
      var line = el("div", "call " + status);
      line.appendChild(el("span", "call-mark", status === "ok" ? "✓" : status === "error" ? "✕" : "…"));
      line.appendChild(el("span", "call-name", toolTitle(e.name) + (e.source === "mcp" ? " · MCP" : "")));
      line.appendChild(el("span", "call-arg", toolArg(e)));
      line.addEventListener("click", function () { openCalls[e.seq] = !openCalls[e.seq]; scheduleRender(); });
      box.appendChild(line);
      if (status === "error") {
        var retried = item.tools.slice(i + 1).some(function (later) { return later.name === e.name && later.status === "ok"; });
        box.appendChild(el("div", "call-error", ((e.result || {}).error || t("failed")) + (retried ? " — " + t("retried") : "")));
      }
      if (openCalls[e.seq]) box.appendChild(el("pre", "json", JSON.stringify({args: e.args, result: e.result}, null, 2)));
    });
    return box;
  }

  function renderChange(c) {
    var row = el("div", "change" + (c.inherited ? " inherited" : ""));
    var known = LABELS[c.key];
    var label = lang === "ru" ? (c.label_local && c.label_local !== c.key ? c.label_local : known ? known[0] : c.label || c.key)
                              : (c.label && c.label !== c.key ? c.label : known ? known[1] : c.key);
    row.appendChild(el("span", "label", label));
    if (c.scope && c.scope !== "global") {
      row.appendChild(el("span", "scope", lang === "ru" ? c.scope.replace(/^extruder /, "экструдер ") : c.scope));
    }
    var shownUnit = c.unit === "count" || c.unit === "fraction" ? "" : c.unit;  // core's internal units
    var unit = shownUnit && shownUnit !== "%" ? " " + shownUnit : (shownUnit === "%" ? "%" : "");
    if (c.old !== null && c.old !== undefined) {
      row.appendChild(el("span", "old", (c.old_text || fmt(c.old)) + unit));
      row.appendChild(el("span", "arrow", "→"));
    }
    row.appendChild(el("span", "new", c.new === null || c.new === undefined ? "—" : (c.new_text || fmt(c.new)) + unit));
    return row;
  }

  function renderQuestion(e) {
    var node = el("div", "ask " + (e.status || "pending"));
    node.appendChild(el("div", "ask-q", e.text || ""));
    if (e.status === "answered") {
      node.appendChild(el("div", "ask-a", e.answer || ""));
      return node;
    }
    if (e.status === "cancelled") {
      node.appendChild(el("div", "ask-hint", t("askCancelled")));
      return node;
    }
    var opts = el("div", "ask-opts");
    (e.options || []).forEach(function (option, index) {
      var b = el("button", "", option);
      b.addEventListener("click", function () { answer(e, option, index); });
      opts.appendChild(b);
    });
    node.appendChild(opts);
    if (e.allow_custom !== false) node.appendChild(el("div", "ask-hint", t("askHint")));
    return node;
  }

  function renderError(e) {
    var node = el("div", "err");
    if (e.code === "not_configured") {
      node.appendChild(document.createTextNode(t("notConfigured")));
      var b = el("button", "", t("openSettings"));
      b.addEventListener("click", openSheet);
      node.appendChild(b);
    } else if (e.code === "too_many_steps") {
      node.textContent = t("tooManySteps");
    } else {
      node.textContent = (e.code === "llm" ? t("llmError") : t("internalError")) + (e.text || "");
    }
    return node;
  }

  function nearBottom() { return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80; }

  function apply(list) {
    list.forEach(function (e) {
      if (e.type === "reset") {
        events = {}; order = []; cache = {}; openDetails = {}; openCalls = {};
        return;
      }
      if (e.type === "patch") {
        var target = events[e.target];
        if (!target) return;
        if (e.data) Object.assign(target, e.data);
        if (e.append) Object.keys(e.append).forEach(function (k) { target[k] = (target[k] || "") + e.append[k]; });
        target._rev = (target._rev || 0) + 1;
        return;
      }
      if (!events[e.seq]) order.push(e.seq);
      e._rev = 0;
      events[e.seq] = e;
    });
    scheduleRender();
  }

  function rerenderAll() { cache = {}; scheduleRender(); }

  function setBusy(value) {
    if (busy !== !!value) { busy = !!value; scheduleRender(); }
    updateComposer();
  }

  function updateComposer() {
    var answering = !!question;
    var sendButton = $("send");
    sendButton.classList.toggle("busy", busy && !answering);
    sendButton.title = busy && !answering ? t("stop") : t("send");
    input.placeholder = answering ? t("answerPlaceholder") : t("placeholder");
  }

  // ---------------------------------------------------------------- polling

  function poll() {
    api("/api/events?since=" + seq).then(function (data) {
      $("link").textContent = "";
      apply(data.events || []);
      seq = data.seq;
      setBusy(data.busy);
      poll();
    }).catch(function () {
      $("link").textContent = t("offline");
      setTimeout(poll, 1500);
    });
  }

  function start() {
    api("/api/state").then(function (data) {
      applySettings(data.settings);
      $("mcpUrl").textContent = data.mcp_url || "";
      $("mcpToken").textContent = "Authorization: Bearer " + token;
      apply(data.events || []);
      seq = data.seq;
      setBusy(data.busy);
      requestAnimationFrame(function () { feed.scrollTop = feed.scrollHeight; });
      poll();
    }).catch(function () {
      $("link").textContent = t("offline");
      setTimeout(start, 1500);
    });
  }

  // ---------------------------------------------------------------- composer

  var input = $("input");

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  }

  var UPLOAD_ERRORS = {unsupported: "upUnsupported", empty: "upEmpty", too_large: "upLarge", unreadable: "upBroken",
                       incomplete: "upNetwork"};

  function renderChips() {
    var box = $("chips");
    box.textContent = "";
    pending.forEach(function (p) {
      var chip = el("div", "chip" + (p.state === "loading" ? " loading" : p.state === "bad" ? " bad" : ""));
      var label = (p.kind === "spec" ? t("spec") + " · " : "") + p.name;
      chip.appendChild(el("span", "", label));
      if (p.state === "bad") chip.appendChild(el("em", "", t(p.reason || "upNetwork")));
      var x = el("button", "", "×");
      x.addEventListener("click", function () { pending.splice(pending.indexOf(p), 1); renderChips(); });
      chip.appendChild(x);
      box.appendChild(chip);
    });
  }

  function upload(files) {
    Array.prototype.forEach.call(files, function (file) {
      var item = {name: file.name, state: "loading", id: null, kind: null, reason: null};
      pending.push(item);
      // Read first: files dragged out of a ZIP or a cloud folder can fail here, before any upload.
      file.arrayBuffer().then(function (buffer) {
        if (!buffer.byteLength) { var e = new Error("empty"); e.code = "empty"; throw e; }
        return api("/api/upload?name=" + encodeURIComponent(file.name), {method: "POST", body: buffer})
          .catch(function (e) { if (!e.code) e.code = "network"; throw e; });
      }, function () {
        var e = new Error("unreadable"); e.code = "read"; throw e;
      }).then(function (data) {
        item.id = data.id; item.kind = data.kind; item.state = "ready";
      }).catch(function (e) {
        item.state = "bad";
        item.reason = e.code === "read" ? "upUnreadable" : e.code === "network" ? "upNetwork" :
          UPLOAD_ERRORS[e.code] || "upNetwork";
      }).then(renderChips);
    });
    renderChips();
  }

  function answer(e, text, choice) {
    api("/api/answer", {method: "POST", json: {seq: e.seq, text: text, choice: choice}}).catch(function () {});
  }

  function send() {
    var text = input.value.trim();
    if (question) {
      if (text) { answer(question, text, null); input.value = ""; autosize(); }
      return;  // an empty Enter while a question waits must not cancel the task
    }
    if (busy) { api("/api/cancel", {method: "POST", json: {}}); return; }
    if (pending.some(function (p) { return p.state === "loading"; })) return;
    var ids = pending.filter(function (p) { return p.state === "ready"; }).map(function (p) { return p.id; });
    if (!text && !ids.length) return;
    setBusy(true);
    api("/api/message", {method: "POST", json: {text: text, attachments: ids}}).then(function () {
      input.value = ""; pending = pending.filter(function (p) { return p.state === "bad"; }); renderChips(); autosize();
    }).catch(function (e) {
      setBusy(false);
      if (e.status === 409) $("link").textContent = t("busy");
    });
  }

  input.addEventListener("input", autosize);
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) { ev.preventDefault(); send(); }
  });
  input.addEventListener("paste", function (ev) {
    var files = ev.clipboardData && ev.clipboardData.files;
    if (files && files.length) { ev.preventDefault(); upload(files); }
  });
  $("send").addEventListener("click", send);
  $("attach").addEventListener("click", function () { $("file").click(); });
  $("file").addEventListener("change", function () { upload(this.files); this.value = ""; });
  $("newChat").addEventListener("click", function () { api("/api/reset", {method: "POST", json: {}}); });

  var dragDepth = 0;
  window.addEventListener("dragenter", function (ev) { ev.preventDefault(); dragDepth++; $("drop").hidden = false; });
  window.addEventListener("dragleave", function () { if (--dragDepth <= 0) { dragDepth = 0; $("drop").hidden = true; } });
  window.addEventListener("dragover", function (ev) { ev.preventDefault(); });
  window.addEventListener("drop", function (ev) {
    ev.preventDefault(); dragDepth = 0; $("drop").hidden = true;
    if (ev.dataTransfer && ev.dataTransfer.files.length) upload(ev.dataTransfer.files);
  });

  // ---------------------------------------------------------------- settings sheet

  var form = $("form");

  function selectSeg(name, value) {
    form.querySelectorAll('.seg[data-name="' + name + '"] button').forEach(function (b) {
      b.classList.toggle("on", b.dataset.value === value);
    });
  }

  function segValue(name) {
    var on = form.querySelector('.seg[data-name="' + name + '"] button.on');
    return on ? on.dataset.value : undefined;
  }

  function openSheet() {
    form.base_url.value = settings.base_url || "";
    form.model.value = settings.model || "";
    form.worker_python.value = settings.worker_python || "";
    form.planning_inputs_file.value = settings.planning_inputs_file || "";
    form.output_dir.value = settings.output_dir || "";
    form.api_key.value = "";
    form.api_key.placeholder = settings.has_key ? t("keyKept") + " " + (settings.key_hint || "") : "";
    selectSeg("language", lang);
    selectSeg("theme", settings.theme === "dark" ? "dark" : "light");
    $("formNote").textContent = "";
    $("sheet").hidden = false;
  }

  function collect() {
    return {base_url: form.base_url.value, model: form.model.value, output_dir: form.output_dir.value,
            worker_python: form.worker_python.value,
            planning_inputs_file: form.planning_inputs_file.value,
      api_key: form.api_key.value, language: segValue("language"), theme: segValue("theme")};
  }

  function save() {
    return api("/api/settings", {method: "POST", json: collect()}).then(function (data) {
      applySettings(data.settings);
      rerenderAll();
      form.api_key.value = "";
      form.api_key.placeholder = settings.has_key ? t("keyKept") + " " + (settings.key_hint || "") : "";
    });
  }

  form.querySelectorAll(".seg button").forEach(function (b) {
    b.addEventListener("click", function () {
      selectSeg(b.parentNode.dataset.name, b.dataset.value);
      if (b.parentNode.dataset.name === "theme") document.documentElement.dataset.theme = b.dataset.value;
    });
  });
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    save().then(function () { $("formNote").textContent = t("saved"); })
      .catch(function (e) { $("formNote").textContent = t("internalError") + e.message; });
  });
  $("test").addEventListener("click", function () {
    $("formNote").textContent = t("testing");
    save().then(function () { return api("/api/test", {method: "POST", json: {}}); }).then(function (data) {
      $("formNote").textContent = data.ok ? t("testOk") : t("llmError") + data.error;
    }).catch(function (e) { $("formNote").textContent = t("internalError") + e.message; });
  });
  $("menu").addEventListener("click", openSheet);
  $("sheetClose").addEventListener("click", function () {
    $("sheet").hidden = true;
    applySettings(settings);
  });

  applyTexts();
  start();
})();
