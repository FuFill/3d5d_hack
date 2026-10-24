"use strict";

(function () {
  var I18N = {
    ru: {
      title: "Ассистент", newChat: "Новый диалог", settings: "Настройки", close: "Закрыть",
      emptyTitle: "Опишите задачу печати", emptyHint: "Приложите модель STL, OBJ, 3MF или фото",
      placeholder: "Например: кронштейн под нагрузку, PETG, без поддержек", attach: "Прикрепить файл",
      send: "Отправить", stop: "Остановить", drop: "Отпустите файлы",
      baseUrl: "URL API", apiKey: "Ключ API", model: "Модель", outputDir: "Папка для G-code и проектов",
      language: "Язык", theme: "Тема", light: "Светлая", dark: "Тёмная", save: "Сохранить", test: "Проверить",
      saved: "Сохранено", testing: "Проверка…", testOk: "Связь с моделью есть", keyKept: "Ключ сохранён",
      thinking: "Думает", thought: "Ход мыслей", offline: "нет связи со слайсером",
      notConfigured: "Укажите URL, ключ и модель в настройках.", openSettings: "Открыть настройки",
      llmError: "Ошибка модели: ", internalError: "Ошибка: ", tooManySteps: "Слишком много шагов, задача остановлена.",
      stopped: "Остановлено", uploadFailed: "не загружен", busy: "Ассистент ещё работает",
      done: "Готово", slicing: "Слайсинг", failed: "Ошибка"
    },
    en: {
      title: "Assistant", newChat: "New chat", settings: "Settings", close: "Close",
      emptyTitle: "Describe the print job", emptyHint: "Attach an STL, OBJ, 3MF model or a photo",
      placeholder: "E.g. load-bearing bracket, PETG, no supports", attach: "Attach file",
      send: "Send", stop: "Stop", drop: "Drop files here",
      baseUrl: "API URL", apiKey: "API key", model: "Model", outputDir: "Folder for G-code and projects",
      language: "Language", theme: "Theme", light: "Light", dark: "Dark", save: "Save", test: "Test",
      saved: "Saved", testing: "Testing…", testOk: "Model is reachable", keyKept: "Key is stored",
      thinking: "Thinking", thought: "Reasoning", offline: "slicer is not reachable",
      notConfigured: "Set the URL, key and model in settings.", openSettings: "Open settings",
      llmError: "Model error: ", internalError: "Error: ", tooManySteps: "Too many steps, the task was stopped.",
      stopped: "Stopped", uploadFailed: "not uploaded", busy: "The assistant is still working",
      done: "Done", slicing: "Slicing", failed: "Failed"
    }
  };

  var TOOLS = {
    get_state: ["Состояние слайсера", "Slicer state"],
    find_settings: ["Поиск настроек", "Find settings"],
    get_settings: ["Чтение настроек", "Read settings"],
    set_settings: ["Изменение настроек", "Change settings"],
    set_print_mode: ["Режим печати", "Printing mode"],
    list_profiles: ["Список профилей", "List profiles"],
    select_profile: ["Выбор профиля", "Select profile"],
    create_profile: ["Создание профиля", "Create profile"],
    save_profile: ["Сохранение профиля", "Save profile"],
    list_materials: ["Список материалов", "List materials"],
    set_material: ["Выбор материала", "Select material"],
    load_model: ["Загрузка модели", "Load model"],
    list_objects: ["Объекты сцены", "Scene objects"],
    move_object: ["Перемещение", "Move"],
    rotate_object: ["Поворот", "Rotate"],
    scale_object: ["Масштаб", "Scale"],
    arrange_all: ["Авторасстановка", "Arrange all"],
    delete_object: ["Удаление", "Delete"],
    slice: ["Слайсинг", "Slice"],
    get_slice_status: ["Статус слайсинга", "Slice status"],
    get_slice_result: ["Результат слайсинга", "Slice result"],
    export_gcode: ["Экспорт G-code", "Export G-code"],
    save_project: ["Сохранение проекта 3MF", "Save 3MF project"]
  };

  var token = (location.hash.match(/t=([0-9a-f]+)/) || [])[1] || "";
  var lang = "ru";
  var seq = 0;
  var busy = false;
  var settings = {};
  var events = {};
  var nodes = {};
  var openDetails = {};
  var pending = [];
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
        if (!r.ok) { var e = new Error(data.error || r.status); e.status = r.status; throw e; }
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
    $("send").title = busy ? t("stop") : t("send");
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
      case "find_settings": return a.query || "";
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

  // ---------------------------------------------------------------- rendering

  function render(e) {
    var node = nodes[e.seq], fresh = !node;
    switch (e.type) {
      case "user": node = node || renderUser(e); break;
      case "thinking": node = renderThinking(e, node); break;
      case "message": node = renderMessage(e, node); break;
      case "tool": node = renderTool(e, node, fresh); break;
      case "error": node = node || renderError(e); break;
      case "done": node = node || (e.status === "cancelled" ? el("div", "stopped", t("stopped")) : null); break;
      default: node = null;
    }
    if (node && fresh) {
      nodes[e.seq] = node;
      feed.appendChild(node);
    }
  }

  function renderUser(e) {
    var node = el("div", "msg user", e.text || "");
    if (e.attachments && e.attachments.length) {
      var files = el("div", "files");
      e.attachments.forEach(function (a) { files.appendChild(el("span", "", (a.kind === "image" ? "▣ " : "◇ ") + a.name)); });
      node.appendChild(files);
    }
    return node;
  }

  function renderThinking(e, node) {
    if (!node) {
      node = el("details", "think");
      node.appendChild(el("summary"));
      node.appendChild(el("div", "think-body"));
    }
    node.classList.toggle("streaming", !!e.streaming);
    node.firstChild.textContent = e.streaming ? t("thinking") : t("thought");
    node.lastChild.textContent = e.text || "";
    return node;
  }

  function renderMessage(e, node) {
    node = node || el("div", "msg ai");
    node.classList.toggle("streaming", !!e.streaming);
    node.innerHTML = markdown(e.text || "");
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

  function renderTool(e, node, fresh) {
    if (!node) {
      node = el("div", "tool");
      var row = el("div", "tool-row");
      row.appendChild(el("span", "dot"));
      row.appendChild(el("span", "tool-name"));
      row.appendChild(el("span", "tool-arg"));
      var more = el("button", "more", "⋯");
      more.title = "JSON";
      more.addEventListener("click", function () {
        openDetails[e.seq] = !openDetails[e.seq];
        renderTool(events[e.seq], node, false);
      });
      row.appendChild(more);
      node.appendChild(row);
      node.appendChild(el("div", "tool-body"));
    }
    var status = e.status || "running";
    node.className = "tool " + status;
    var row = node.firstChild;
    row.children[1].textContent = toolTitle(e.name) + (e.source === "mcp" ? " · MCP" : "");
    row.children[2].textContent = toolArg(e);
    row.children[3].classList.toggle("on", !!openDetails[e.seq]);

    var body = node.lastChild;
    var hadChanges = !!body.querySelector(".changes");
    body.textContent = "";
    var r = e.result || {};

    if (e.name === "slice" && status === "running") {
      var wrap = el("div", "bar-wrap");
      var track = el("div", "track"), fill = el("div", "fill");
      fill.style.width = Math.round((e.progress || 0) * 100) + "%";
      track.appendChild(fill);
      wrap.appendChild(track);
      wrap.appendChild(el("span", "", Math.round((e.progress || 0) * 100) + "%"));
      body.appendChild(wrap);
    }

    if (status === "error") {
      body.appendChild(el("div", "tool-note", r.error || t("failed")));
    } else if (status === "ok") {
      var changes = r.changes || [];
      if (changes.length) {
        var box = el("div", "changes" + (!hadChanges && !fresh ? " fresh" : ""));
        changes.forEach(function (c) { box.appendChild(renderChange(c)); });
        body.appendChild(box);
      }
      if (e.name === "slice" || e.name === "get_slice_result") {
        var parts = [r.print_time, r.material_g ? r.material_g + (lang === "ru" ? " г" : " g") : "",
          r.material_m ? r.material_m + (lang === "ru" ? " м" : " m") : ""].filter(Boolean);
        body.appendChild(el("div", "summary", parts.join(" · ") || t("done")));
      }
      if (r.path) body.appendChild(el("div", "tool-note path", r.path));
      if (r.warnings && r.warnings.length) body.appendChild(el("div", "tool-note", "! " + r.warnings.join("; ")));
      if (r.errors && r.errors.length) body.appendChild(el("div", "tool-note", "✕ " + r.errors.join("; ")));
    }

    if (openDetails[e.seq]) {
      var text = JSON.stringify({args: e.args, result: e.result}, null, 2);
      body.appendChild(el("pre", "details", text));
    }
    return node;
  }

  function renderChange(c) {
    var row = el("div", "change");
    row.appendChild(el("span", "label", lang === "ru" ? (c.label_local || c.label) : (c.label || c.key)));
    if (c.scope && c.scope !== "global") {
      row.appendChild(el("span", "scope", lang === "ru" ? c.scope.replace(/^extruder /, "экструдер ") : c.scope));
    }
    var unit = c.unit && c.unit !== "%" ? " " + c.unit : (c.unit === "%" ? "%" : "");
    if (c.old !== null && c.old !== undefined) {
      row.appendChild(el("span", "old", (c.old_text || fmt(c.old)) + unit));
      row.appendChild(el("span", "arrow", "→"));
    }
    row.appendChild(el("span", "new", c.new === null || c.new === undefined ? "—" : (c.new_text || fmt(c.new)) + unit));
    return row;
  }

  function nearBottom() { return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80; }

  function apply(list, initial) {
    var stick = initial || nearBottom();
    list.forEach(function (e) {
      if (e.type === "reset") {
        events = {}; nodes = {}; openDetails = {};
        Array.prototype.slice.call(feed.querySelectorAll(":scope > :not(.empty)")).forEach(function (n) { n.remove(); });
        return;
      }
      if (e.type === "patch") {
        var target = events[e.target];
        if (!target) return;
        if (e.data) Object.assign(target, e.data);
        if (e.append) Object.keys(e.append).forEach(function (k) { target[k] = (target[k] || "") + e.append[k]; });
        render(target);
        return;
      }
      events[e.seq] = e;
      render(e);
    });
    $("empty").hidden = Object.keys(nodes).length > 0;
    if (stick) feed.scrollTop = feed.scrollHeight;
  }

  function rerenderAll() {
    var list = Object.keys(events).map(Number).sort(function (a, b) { return a - b; }).map(function (k) { return events[k]; });
    events = {}; nodes = {};
    Array.prototype.slice.call(feed.querySelectorAll(":scope > :not(.empty)")).forEach(function (n) { n.remove(); });
    apply(list, true);
  }

  function setBusy(value) {
    busy = !!value;
    $("send").classList.toggle("busy", busy);
    $("send").title = busy ? t("stop") : t("send");
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
      apply(data.events || [], true);
      seq = data.seq;
      setBusy(data.busy);
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

  function renderChips() {
    var box = $("chips");
    box.textContent = "";
    pending.forEach(function (p) {
      var chip = el("div", "chip" + (p.state === "loading" ? " loading" : p.state === "bad" ? " bad" : ""));
      chip.appendChild(el("span", "", p.name + (p.state === "bad" ? " — " + t("uploadFailed") : "")));
      var x = el("button", "", "×");
      x.addEventListener("click", function () { pending.splice(pending.indexOf(p), 1); renderChips(); });
      chip.appendChild(x);
      box.appendChild(chip);
    });
  }

  function upload(files) {
    Array.prototype.forEach.call(files, function (file) {
      var item = {name: file.name, state: "loading", id: null};
      pending.push(item);
      api("/api/upload?name=" + encodeURIComponent(file.name), {method: "POST", body: file})
        .then(function (data) { item.id = data.id; item.state = "ready"; })
        .catch(function () { item.state = "bad"; })
        .then(renderChips);
    });
    renderChips();
  }

  function send() {
    if (busy) { api("/api/cancel", {method: "POST", json: {}}); return; }
    if (pending.some(function (p) { return p.state === "loading"; })) return;
    var text = input.value.trim();
    var ids = pending.filter(function (p) { return p.state === "ready"; }).map(function (p) { return p.id; });
    if (!text && !ids.length) return;
    setBusy(true);
    api("/api/message", {method: "POST", json: {text: text, attachments: ids}}).then(function () {
      input.value = ""; pending = []; renderChips(); autosize();
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
