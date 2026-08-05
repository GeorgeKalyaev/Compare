#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import html
import copy
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ULTIMATE_MARKERS = (
    "kg.apc.jmeter.threads.UltimateThreadGroup",
    "UltimateThreadGroup",
    "Ultimate Thread Group",
)
IGNORED_ATTRIBUTES = {"guiclass", "testclass", "testname", "enabled"}

# Эти элементы и весь их дочерний hashTree полностью исключаются из сравнения.
IGNORED_ELEMENT_TYPES = {
    "TestAction",                 # Flow Control Action
    "ConstantThroughputTimer",
}

IGNORED_ELEMENT_NAMES = {
    "Flow Control Action",
    "Constant Throughput Timer",
}


@dataclass(frozen=True)
class GroupInfo:
    name: str
    key: str
    enabled: bool
    tree: ET.Element
    index: int


@dataclass(frozen=True)
class NodeInfo:
    path: str
    enabled: bool
    properties: dict[str, str]


def local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def is_hash_tree(element: ET.Element) -> bool:
    return local_tag(element.tag) == "hashTree"


def is_ultimate_group(element: ET.Element) -> bool:
    text = " ".join((
        local_tag(element.tag),
        element.attrib.get("testclass", ""),
        element.attrib.get("guiclass", ""),
    )).lower()
    return any(marker.lower() in text for marker in ULTIMATE_MARKERS)


def enabled(element: ET.Element) -> bool:
    return element.attrib.get("enabled", "true").strip().lower() != "false"


def element_type(element: ET.Element) -> str:
    return element.attrib.get("testclass") or local_tag(element.tag) or "(неизвестный тип)"


def element_name(element: ET.Element) -> str:
    return element.attrib.get("testname", "").strip() or "(без имени)"


def clean(value: str | None) -> str:
    if value is None:
        return ""
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines = [line.rstrip() for line in lines]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def short(value: str, limit: int = 180) -> str:
    value = value.replace("\n", "\\n")
    return value if len(value) <= limit else value[: limit - 3] + "..."


def uc_key(name: str) -> str:
    numbers = re.findall(r"(?i)UC[\s_-]*(\d+)", name)
    if numbers:
        result: list[str] = []
        seen: set[str] = set()
        for number in numbers:
            normalized = str(int(number))
            if normalized not in seen:
                seen.add(normalized)
                result.append(f"UC{normalized}")
        return "+".join(result)
    return re.sub(r"\s+", " ", name.casefold()).strip() or "(без имени)"



def is_ignored_element(element: ET.Element) -> bool:
    """Проверяет, нужно ли полностью исключить элемент из сравнения."""
    node_type = element_type(element).strip().casefold()
    node_name = element_name(element).strip().casefold()

    ignored_types = {value.casefold() for value in IGNORED_ELEMENT_TYPES}
    ignored_names = {value.casefold() for value in IGNORED_ELEMENT_NAMES}

    return node_type in ignored_types or node_name in ignored_names

def property_label(element: ET.Element, index: int) -> str:
    tag = local_tag(element.tag)
    name = (
        element.attrib.get("name")
        or element.attrib.get("elementType")
        or element.attrib.get("testname")
        or str(index)
    )
    return f"{tag}[{name}]"


def flatten_properties(element: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in sorted(element.attrib.items()):
        if name not in IGNORED_ATTRIBUTES:
            result[f"@{name}"] = clean(value)

    def walk(current: ET.Element, prefix: str) -> None:
        repeated: Counter[str] = Counter()
        for index, child in enumerate(list(current)):
            if is_hash_tree(child):
                continue
            base = property_label(child, index)
            repeated[base] += 1
            label = base if repeated[base] == 1 else f"{base}#{repeated[base]}"
            path = f"{prefix}/{label}" if prefix else label
            text = clean(child.text)
            if text:
                result[f"{path}/#text"] = text
            for name, value in sorted(child.attrib.items()):
                if name not in IGNORED_ATTRIBUTES:
                    result[f"{path}/@{name}"] = clean(value)
            walk(child, path)

    walk(element, "")
    return result


def iter_pairs(tree: ET.Element) -> Iterable[tuple[ET.Element, ET.Element | None]]:
    children = list(tree)
    index = 0
    while index < len(children):
        current = children[index]
        if is_hash_tree(current):
            index += 1
            continue
        child_tree = None
        if index + 1 < len(children) and is_hash_tree(children[index + 1]):
            child_tree = children[index + 1]
            index += 2
        else:
            index += 1
        yield current, child_tree


def build_inventory(tree: ET.Element, include_disabled: bool) -> dict[str, NodeInfo]:
    """
    Строит инвентарь элементов.

    В активном режиме номера повторов считаются только среди реально активных
    элементов. Поэтому ситуация «два отключённых sampler + один включённый»
    сопоставляется с единственным включённым sampler в Stability без ложного [#3].
    """
    result: dict[str, NodeInfo] = {}

    def walk(
        current_tree: ET.Element,
        parent: str,
        parent_effectively_enabled: bool,
    ) -> None:
        active_sibling_counts: Counter[str] = Counter()
        all_sibling_counts: Counter[str] = Counter()

        for element, child_tree in iter_pairs(current_tree):
            # Flow Control Action и Constant Throughput Timer игнорируются
            # вместе со всем, что вложено в их дочерний hashTree.
            if is_ignored_element(element):
                continue

            base = f'{element_type(element)} "{element_name(element)}"'
            own_enabled = enabled(element)
            effective_enabled = parent_effectively_enabled and own_enabled

            if include_disabled:
                all_sibling_counts[base] += 1
                occurrence = all_sibling_counts[base]
            else:
                if effective_enabled:
                    active_sibling_counts[base] += 1
                    occurrence = active_sibling_counts[base]
                else:
                    occurrence = 0

            if occurrence:
                segment = base if occurrence == 1 else f"{base} [#{occurrence}]"
                path = f"{parent} / {segment}" if parent else segment
                result[path] = NodeInfo(
                    path=path,
                    enabled=effective_enabled,
                    properties=flatten_properties(element),
                )
            else:
                # Отключённый элемент не входит в активный инвентарь.
                # Его дочерняя ветка тоже считается неактивной.
                path = f"{parent} / {base}" if parent else base

            if child_tree is not None:
                walk(child_tree, path, effective_enabled)

    walk(tree, "", True)
    return result


def extract_groups(path: Path) -> dict[str, list[GroupInfo]]:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise RuntimeError(f"Не удалось разобрать XML в '{path}': {error}") from error
    except OSError as error:
        raise RuntimeError(f"Не удалось открыть '{path}': {error}") from error

    groups: dict[str, list[GroupInfo]] = defaultdict(list)
    group_index = 0
    for parent in root.iter():
        children = list(parent)
        for index, child in enumerate(children):
            if not is_ultimate_group(child):
                continue
            name = element_name(child)
            tree = (
                copy.deepcopy(children[index + 1])
                if index + 1 < len(children) and is_hash_tree(children[index + 1])
                else ET.Element("hashTree")
            )
            groups[uc_key(name)].append(
                GroupInfo(name=name, key=uc_key(name), enabled=enabled(child), tree=tree, index=group_index)
            )
            group_index += 1
    return groups


def active_paths(group: GroupInfo) -> set[str]:
    return set(build_inventory(group.tree, include_disabled=False))


def similarity(maxperf: GroupInfo, stability: GroupInfo) -> float:
    a = active_paths(maxperf)
    b = set(build_inventory(stability.tree, include_disabled=True))
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def choose_match(maxperf: GroupInfo, candidates: list[GroupInfo], used: set[int]) -> GroupInfo | None:
    available = [item for item in candidates if item.index not in used]
    if not available:
        return None
    available.sort(key=lambda item: (-similarity(maxperf, item), 0 if item.enabled else 1, abs(maxperf.index - item.index)))
    selected = available[0]
    used.add(selected.index)
    return selected


def display_property(key: str) -> str:
    aliases = {
        "@testname": "Название",
        "@enabled": "enabled",
        "stringProp[HTTPSampler.path]/#text": "HTTP path",
        "stringProp[HTTPSampler.method]/#text": "HTTP method",
        "stringProp[HTTPSampler.domain]/#text": "HTTP domain",
        "stringProp[HTTPSampler.port]/#text": "HTTP port",
        "stringProp[HTTPSampler.protocol]/#text": "HTTP protocol",
        "stringProp[script]/#text": "script",
        "stringProp[filename]/#text": "filename",
    }
    return aliases.get(key, key)


def compare_properties(max_node: NodeInfo, stability_node: NodeInfo) -> list[str]:
    lines: list[str] = []
    keys = sorted(set(max_node.properties) | set(stability_node.properties), key=str.casefold)
    for key in keys:
        max_value = max_node.properties.get(key)
        stability_value = stability_node.properties.get(key)
        if max_value == stability_value:
            continue
        label = display_property(key)
        if stability_value is None:
            lines.append(f"      + В Stability отсутствует {label}; MaxPerf: {short(max_value or '')}")
        elif max_value is None:
            lines.append(f"      - В Stability лишнее свойство {label}: {short(stability_value)}")
        else:
            lines.extend([
                f"      * {label}",
                f"          Stability: {short(stability_value)}",
                f"          MaxPerf:   {short(max_value)}",
            ])
    return lines



_OCCURRENCE_RE = re.compile(r" \[#\d+\]")


def canonical_path(path: str) -> str:
    """Убирает технические номера одинаковых соседних элементов."""
    return _OCCURRENCE_RE.sub("", path)


def index_by_canonical(
    inventory: dict[str, NodeInfo],
) -> dict[str, list[NodeInfo]]:
    result: dict[str, list[NodeInfo]] = defaultdict(list)
    for node in inventory.values():
        result[canonical_path(node.path)].append(node)
    return result

def compare_group(maxperf: GroupInfo, stability: GroupInfo) -> list[str]:
    """
    Показывает только различия, которые нужны для приведения Stability к MaxPerf.

    Отключённые элементы MaxPerf полностью игнорируются, включая их дочерние
    элементы. Активные элементы сопоставляются без учёта позиции среди
    отключённых дубликатов.
    """
    issues: list[str] = []

    if not stability.enabled:
        issues.append(
            "  ! THREAD GROUP ОТКЛЮЧЕНА В STABILITY, но включена в MaxPerf"
        )

    if maxperf.name != stability.name:
        issues.extend([
            "  ! Отличается название:",
            f"      Stability: {stability.name}",
            f"      MaxPerf:   {maxperf.name}",
        ])

    max_active = build_inventory(maxperf.tree, include_disabled=False)
    stability_active = build_inventory(stability.tree, include_disabled=False)
    stability_all = build_inventory(stability.tree, include_disabled=True)

    active_by_canonical = index_by_canonical(stability_active)
    all_by_canonical = index_by_canonical(stability_all)
    used_active_nodes: set[str] = set()
    used_all_nodes: set[str] = set()

    for path, max_node in max_active.items():
        stability_node = stability_active.get(path)

        if stability_node is not None:
            used_active_nodes.add(stability_node.path)
        else:
            key = canonical_path(path)

            # Сначала ищем активный аналог с тем же смысловым путём,
            # даже если номера [#2]/[#3] отличаются.
            active_candidates = [
                node
                for node in active_by_canonical.get(key, [])
                if node.path not in used_active_nodes
            ]

            if active_candidates:
                stability_node = active_candidates[0]
                used_active_nodes.add(stability_node.path)
            else:
                # Затем проверяем, существует ли такой элемент, но отключён.
                disabled_candidates = [
                    node
                    for node in all_by_canonical.get(key, [])
                    if not node.enabled and node.path not in used_all_nodes
                ]

                if disabled_candidates:
                    disabled_node = disabled_candidates[0]
                    used_all_nodes.add(disabled_node.path)
                    issues.append(f"  ! ОТКЛЮЧЕН В STABILITY: {path}")
                    continue

                issues.append(f"  + НЕТ В STABILITY: {path}")
                continue

        changes = compare_properties(max_node, stability_node)
        if changes:
            issues.append(f"  * ИЗМЕНЁН: {path}")
            issues.extend(changes)

    return issues


def compare_files(maxperf_path: Path, stability_path: Path, output_path: Path) -> int:
    maxperf_groups = extract_groups(maxperf_path)
    stability_groups = extract_groups(stability_path)
    active_maxperf = sorted(
        [group for values in maxperf_groups.values() for group in values if group.enabled],
        key=lambda group: group.index,
    )

    def esc(value: object) -> str:
        return html.escape(str(value), quote=True)

    def line_to_html(line: str) -> str:
        stripped = line.strip()
        if stripped.startswith("!"):
            css = "issue warning"
        elif stripped.startswith("+"):
            css = "issue missing"
        elif stripped.startswith("*"):
            css = "issue changed"
        elif stripped.startswith("Stability:"):
            css = "detail stability"
        elif stripped.startswith("MaxPerf:"):
            css = "detail maxperf"
        else:
            css = "detail"
        return f'<div class="{css}">{esc(stripped)}</div>'

    used: set[int] = set()
    ok = needs_work = missing = 0
    cards: list[str] = []

    for number, max_group in enumerate(active_maxperf, start=1):
        match = choose_match(max_group, stability_groups.get(max_group.key, []), used)

        if match is None:
            missing += 1
            cards.append(f"""
            <section class="card card-missing">
              <div class="card-header">
                <div>
                  <div class="uc">{number}. {esc(max_group.key)}</div>
                  <h2>{esc(max_group.name)}</h2>
                </div>
                <span class="badge badge-missing">Нет в Stability</span>
              </div>
              <div class="action">Добавить или восстановить соответствующую Thread Group в Stability.</div>
            </section>
            """)
            continue

        issues = compare_group(max_group, match)
        if not issues:
            ok += 1
            continue

        needs_work += 1
        issues_html = "\n".join(line_to_html(line) for line in issues)
        cards.append(f"""
        <section class="card">
          <div class="card-header">
            <div>
              <div class="uc">{number}. {esc(max_group.key)}</div>
              <h2>{esc(max_group.name)}</h2>
              <div class="stability-name">Stability: {esc(match.name)}</div>
            </div>
            <span class="badge badge-review">Требует доработки</span>
          </div>
          <div class="issues">{issues_html}</div>
        </section>
        """)

    total = len(active_maxperf)
    has_problems = bool(needs_work or missing)
    status_class = "summary-problem" if has_problems else "summary-ok"
    status_text = (
        "Найдены отличия, которые стоит проверить"
        if has_problems
        else "Активные сценарии совпадают"
    )

    cards_html = "\n".join(cards) if cards else """
    <section class="empty-state">
      <h2>Различий не найдено</h2>
      <p>Активная часть Stability соответствует активной части MaxPerf.</p>
    </section>
    """

    document = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Сравнение MaxPerf и Stability</title>
<style>
:root {{
  --bg:#f5f6f8; --panel:#fff; --text:#29343c; --muted:#6f7b84;
  --line:#dfe4e8; --accent:#4f6979; --accent-soft:#edf3f6;
  --warn:#84602f; --warn-soft:#faf4e8; --danger:#875050;
  --danger-soft:#f9eeee; --green:#4d6e5a; --green-soft:#edf5f0;
  --shadow:0 4px 14px rgba(30,40,48,.06);
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--text);font-family:"Segoe UI",Arial,sans-serif;line-height:1.5}}
.container{{width:min(1180px,calc(100% - 32px));margin:28px auto 48px}}
.hero,.metric,.card,.empty-state{{background:var(--panel);border:1px solid var(--line);box-shadow:var(--shadow)}}
.hero{{border-radius:14px;padding:24px 26px;margin-bottom:18px}}
h1{{margin:0 0 14px;font-size:26px;font-weight:650}}
.files{{display:grid;gap:7px;color:var(--muted);font-size:14px}}
.files strong{{color:var(--text)}}
.rules{{margin-top:16px;padding:13px 15px;border-radius:10px;background:var(--accent-soft);color:#52646f;font-size:14px}}
.summary{{display:grid;grid-template-columns:repeat(4,minmax(145px,1fr));gap:12px;margin-bottom:18px}}
.metric{{border-radius:12px;padding:16px}}
.metric-value{{font-size:25px;font-weight:700;color:var(--accent)}}
.metric-label{{margin-top:3px;color:var(--muted);font-size:13px}}
.summary-status{{grid-column:1/-1;border-radius:11px;padding:12px 15px;font-weight:600}}
.summary-problem{{background:var(--warn-soft);color:var(--warn);border:1px solid #eadbbb}}
.summary-ok{{background:var(--green-soft);color:var(--green);border:1px solid #d7e7dc}}
.card{{border-left:4px solid #b18a58;border-radius:12px;padding:20px 22px;margin-bottom:14px}}
.card-missing{{border-left-color:#a76464}}
.card-header{{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;padding-bottom:14px;border-bottom:1px solid var(--line)}}
.uc{{color:var(--accent);font-size:13px;font-weight:700;letter-spacing:.04em}}
h2{{margin:3px 0 4px;font-size:18px;font-weight:650;overflow-wrap:anywhere}}
.stability-name{{color:var(--muted);font-size:13px;overflow-wrap:anywhere}}
.badge{{flex:0 0 auto;padding:6px 10px;border-radius:999px;font-size:12px;font-weight:650;white-space:nowrap}}
.badge-review{{color:var(--warn);background:var(--warn-soft);border:1px solid #eadbbb}}
.badge-missing{{color:var(--danger);background:var(--danger-soft);border:1px solid #ebd2d2}}
.issues{{padding-top:13px}}
.issue,.detail{{font-family:Consolas,"Courier New",monospace;font-size:13px;padding:5px 9px;margin:3px 0;border-radius:6px;overflow-wrap:anywhere;white-space:pre-wrap}}
.issue.warning{{background:var(--warn-soft);color:#71532d;border-left:3px solid #c6a36d}}
.issue.missing{{background:var(--danger-soft);color:#754141;border-left:3px solid #bc7a7a}}
.issue.changed{{background:var(--accent-soft);color:#405b69;border-left:3px solid #7794a4}}
.detail{{margin-left:20px;color:#5e6972;background:#f8f9fa}}
.detail.stability{{color:#7a5656;background:#fbf4f4}}
.detail.maxperf{{color:#456555;background:#f1f7f3}}
.action{{margin-top:14px;padding:11px 13px;background:var(--danger-soft);color:#754141;border-radius:8px}}
.empty-state{{border-radius:12px;padding:34px;text-align:center}}
.empty-state h2{{color:var(--green);font-size:21px}}
.empty-state p{{color:var(--muted)}}
.footer{{margin-top:24px;text-align:center;color:var(--muted);font-size:12px}}
@media(max-width:760px){{.summary{{grid-template-columns:repeat(2,1fr)}}.card-header{{flex-direction:column}}.badge{{align-self:flex-start}}}}
@media print{{body{{background:white}}.container{{width:100%;margin:0}}.hero,.metric,.card,.empty-state{{box-shadow:none}}.card{{break-inside:avoid}}}}
</style>
</head>
<body>
<main class="container">
  <section class="hero">
    <h1>Сравнение MaxPerf и Stability</h1>
    <div class="files">
      <div><strong>MaxPerf:</strong> {esc(maxperf_path)}</div>
      <div><strong>Stability:</strong> {esc(stability_path)}</div>
    </div>
    <div class="rules">
      Проверяются только активные Thread Group и активные элементы MaxPerf.
      Отключённые элементы MaxPerf, Flow Control Action, Constant Throughput Timer
      и профиль нагрузки Ultimate Thread Group не сравниваются.
    </div>
  </section>

  <section class="summary">
    <div class="metric"><div class="metric-value">{total}</div><div class="metric-label">Активных Thread Group в MaxPerf</div></div>
    <div class="metric"><div class="metric-value">{ok}</div><div class="metric-label">Полностью совпадают</div></div>
    <div class="metric"><div class="metric-value">{needs_work}</div><div class="metric-label">Требуют доработки</div></div>
    <div class="metric"><div class="metric-value">{missing}</div><div class="metric-label">Отсутствуют в Stability</div></div>
    <div class="summary-status {status_class}">{status_text}</div>
  </section>

  {cards_html}

  <div class="footer">Отчёт сформирован автоматически. MaxPerf используется как эталон.</div>
</main>
</body>
</html>
"""

    try:
        output_path.write_text(document, encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"Не удалось записать HTML-отчёт '{output_path}': {error}") from error

    print("=" * 76)
    print("HTML-ОТЧЁТ СФОРМИРОВАН")
    print("=" * 76)
    print(f"Активных Thread Group в MaxPerf: {total}")
    print(f"Полностью совпадают:             {ok}")
    print(f"Требуют доработки:               {needs_work}")
    print(f"Отсутствуют в Stability:          {missing}")
    print()
    print(f"Отчёт сохранён: {output_path.resolve()}")
    print("Открой HTML-файл двойным щелчком в браузере.")

    return 1 if has_problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="HTML-сравнение активной части MaxPerf со Stability")
    parser.add_argument("maxperf", type=Path, help="MaxPerf JMX-файл")
    parser.add_argument("stability", type=Path, help="Stability JMX-файл")
    parser.add_argument("-o", "--output", type=Path, default=Path("stability_changes.html"), help="HTML-файл отчёта")
    args = parser.parse_args()

    if not args.maxperf.is_file():
        print(f"Ошибка: MaxPerf-файл не найден: {args.maxperf}", file=sys.stderr)
        return 2
    if not args.stability.is_file():
        print(f"Ошибка: Stability-файл не найден: {args.stability}", file=sys.stderr)
        return 2

    try:
        return compare_files(args.maxperf, args.stability, args.output)
    except RuntimeError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
