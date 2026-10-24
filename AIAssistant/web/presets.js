"use strict";

// Presentation only: keep the core's diagnostic strings and compatibility checks intact.
(function (root) {
  var REASONS = {
    "invalid preset catalog": "Не удалось прочитать каталог пресетов",
    "catalog belongs to another machine": "Каталог пресетов относится к другому принтеру",
    "no compatible native quality profile": "Нет совместимого пресета качества STE",
    "every compatible profile conflicts with the validated plan": "Доступные пресеты противоречат требованиям плана",
    "not available for the installed nozzle/material on every enabled extruder": "Не подходит сопло или материал одного из включённых экструдеров",
    "defined for another machine": "Пресет предназначен для другого принтера",
    "allowed custom profile is not installed": "Пользовательский пресет не установлен",
    "plan overrides fail validation": "Изменения поверх пресета не прошли проверку",
    "plan does not satisfy mandatory requirements": "План не удовлетворяет обязательным требованиям ТЗ",
    "material GUID differs from the planner catalog": "Идентификатор материала не совпадает с каталогом ядра"
  };
  var OPTIONS = {
    "install the nozzle physically and select the matching nozzle in STE, then plan again": "Физически установите подходящее сопло, выберите его в STE и повторите расчёт",
    "choose a material compatible with the installed nozzle": "Выберите материал, совместимый с установленным соплом",
    "enable the extruder in STE": "Включите нужный экструдер в STE",
    "install the Stereotech material profile in STE": "Установите профиль материала Stereotech в STE",
    "choose another installed material": "Выберите другой установленный материал",
    "select a material for the installed filament diameter": "Выберите материал под установленный диаметр прутка",
    "select a printer/nozzle/material combination that has quality profiles in STE": "Выберите сочетание принтера, сопла и материала, для которого есть пресеты качества STE",
    "review the rejected overrides": "Проверьте изменения, отклонённые при проверке",
    "allow another installed profile": "Разрешите другой установленный пресет"
  };
  var MODES = {classic: "3D", cylindrical: "5D Spiral", cylindrical_full: "5D"};

  function number(value) { return String(value).replace(".", ","); }
  function reason(text, language) {
    text = String(text || "");
    if (language !== "ru") return text || "Preset is incompatible with the current print conditions";
    if (REASONS[text]) return REASONS[text];
    var m = text.match(/^hardware change required: material needs nozzle >= (\S+) mm, installed (\S+) mm$/);
    if (m) return "Нужно сопло от " + number(m[1]) + " мм; " + (m[2] === "None" ? "диаметр установленного сопла неизвестен" : "установлено " + number(m[2]) + " мм");
    m = text.match(/^planned extruder (.+) is missing or disabled$/);
    if (m) return "Экструдер " + m[1] + " отсутствует или выключен";
    m = text.match(/^material (.+) is not installed\/compatible for extruder (.+) with nozzle (.+)$/);
    if (m) return "Материал " + m[1] + " недоступен для экструдера " + m[2] + " с соплом " + m[3];
    m = text.match(/^ambiguous installed material containers for (.+)$/);
    if (m) return "Найдено несколько профилей материала " + m[1];
    m = text.match(/^filament diameter (.+) mm differs from the extruder (.+) mm$/);
    if (m) return "Диаметр прутка " + number(m[1]) + " мм не совпадает с диаметром экструдера " + number(m[2]) + " мм";
    m = text.match(/^setting_version differs from STE (.+)$/);
    if (m) return "Версия настроек пресета не совпадает с версией STE " + m[1];
    m = text.match(/^its quality_type (.+) is not available here$/);
    if (m) return "Тип качества «" + m[1] + "» недоступен для этого сочетания принтера и материала";
    m = text.match(/^profile fixes printing_mode=(.+), plan requires (.+)$/);
    if (m) return "Пресет рассчитан на " + (MODES[m[1]] || m[1]) + ", а план требует " + (MODES[m[2]] || m[2]);
    // New diagnostic codes remain in the expandable raw tool result, never as English UI copy.
    return /[а-яё]/i.test(text) ? text : "Пресет несовместим с текущими условиями печати";
  }

  function option(text, language) {
    text = String(text || "");
    if (language !== "ru") return text;
    if (OPTIONS[text]) return OPTIONS[text];
    var m = text.match(/^select (.+) in STE or plan for (.+)$/);
    if (m) return "Выберите " + m[1] + " в STE или пересчитайте план для " + m[2];
    m = text.match(/^remove duplicate material profiles: (.+)$/);
    if (m) return "Удалите дублирующие профили материала: " + m[1];
    return /[а-яё]/i.test(text) ? text : "Проверьте совместимость принтера, сопла, материала и пресета в STE";
  }

  function node(doc, tag, cls, text) {
    var n = doc.createElement(tag);
    n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function rejected(doc, item, language) {
    var box = node(doc, "details", "preset-rejected");
    var summary = node(doc, "summary", "preset-rejected-summary");
    summary.appendChild(node(doc, "span", "preset-rejected-label", item.label || (language === "ru" ? "Пресет отклонён" : "Preset rejected")));
    summary.appendChild(node(doc, "span", "preset-rejected-reason", reason(item.reason, language)));
    box.appendChild(summary);
    var body = node(doc, "div", "preset-rejected-options");
    (item.options || []).forEach(function (text) { body.appendChild(node(doc, "div", "", option(text, language))); });
    if (!body.childNodes.length) body.appendChild(node(doc, "div", "", language === "ru" ?
      "Выберите совместимый пресет или измените условия печати" : "Choose a compatible preset or adjust the print conditions"));
    box.appendChild(body);
    return box;
  }

  root.STEPresets = {reason: reason, option: option, rejected: rejected};
})(typeof module === "object" && module.exports ? module.exports : window);
