#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
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
IGNORED_ATTRIBUTES = {"guiclass"}


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
    result: dict[str, NodeInfo] = {}

    def walk(current_tree: ET.Element, parent: str, parent_effectively_enabled: bool) -> None:
        sibling_counts: Counter[str] = Counter()
        for element, child_tree in iter_pairs(current_tree):
            base = f'{element_type(element)} "{element_name(element)}"'
            sibling_counts[base] += 1
            segment = base if sibling_counts[base] == 1 else f"{base} [#{sibling_counts[base]}]"
            path = f"{parent} / {segment}" if parent else segment
            own_enabled = enabled(element)
            effective_enabled = parent_effectively_enabled and own_enabled
            node = NodeInfo(path=path, enabled=effective_enabled, properties=flatten_properties(element))
            if include_disabled or effective_enabled:
                result[path] = node
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


def compare_group(maxperf: GroupInfo, stability: GroupInfo) -> list[str]:
    issues: list[str] = []
    if not stability.enabled:
        issues.append("  ! THREAD GROUP ОТКЛЮЧЕНА В STABILITY, но включена в MaxPerf")
    if maxperf.name != stability.name:
        issues.extend([
            "  ! Отличается название:",
            f"      Stability: {stability.name}",
            f"      MaxPerf:   {maxperf.name}",
        ])

    max_active = build_inventory(maxperf.tree, include_disabled=False)
    stability_all = build_inventory(stability.tree, include_disabled=True)

    for path, max_node in max_active.items():
        stability_node = stability_all.get(path)
        if stability_node is None:
            issues.append(f"  + НЕТ В STABILITY: {path}")
            continue
        if not stability_node.enabled:
            issues.append(f"  ! ОТКЛЮЧЕН В STABILITY: {path}")
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

    report = [
        "=" * 96,
        "ЧТО НУЖНО ИСПРАВИТЬ В STABILITY ПО СРАВНЕНИЮ С MAXPERF",
        "=" * 96,
        f"MaxPerf:   {maxperf_path}",
        f"Stability: {stability_path}",
        "",
        "В отчёт попадают только активные Thread Group и активные элементы MaxPerf.",
        "Отключённые элементы MaxPerf и лишние элементы Stability игнорируются.",
        "Профиль нагрузки Ultimate Thread Group не сравнивается.",
        "",
    ]

    used: set[int] = set()
    ok = needs_work = missing = 0

    for number, max_group in enumerate(active_maxperf, start=1):
        match = choose_match(max_group, stability_groups.get(max_group.key, []), used)
        title = f"{number}. {max_group.key} | MaxPerf: {max_group.name}"

        if match is None:
            missing += 1
            report.extend([
                "-" * 96,
                title,
                "СТАТУС: НЕТ В STABILITY",
                "ДЕЙСТВИЕ: добавить или восстановить соответствующую Thread Group.",
                "",
            ])
            continue

        issues = compare_group(max_group, match)
        if not issues:
            ok += 1
            continue

        needs_work += 1
        report.extend([
            "-" * 96,
            title,
            f"Stability: {match.name}",
            "СТАТУС: ТРЕБУЕТ ДОРАБОТКИ",
            *issues,
            "",
        ])

    report.extend([
        "=" * 96,
        "ИТОГО",
        "=" * 96,
        f"Активных Thread Group в MaxPerf: {len(active_maxperf)}",
        f"Полностью совпадают:             {ok}",
        f"Требуют доработки:               {needs_work}",
        f"Отсутствуют в Stability:          {missing}",
    ])

    if needs_work == 0 and missing == 0:
        report.extend(["", "Активная часть Stability соответствует активной части MaxPerf."])

    text = "\n".join(report) + "\n"
    try:
        output_path.write_text(text, encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"Не удалось записать отчёт '{output_path}': {error}") from error

    print(text)
    print(f"Отчёт сохранён: {output_path.resolve()}")
    return 1 if needs_work or missing else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Краткое сравнение активной части MaxPerf со Stability")
    parser.add_argument("maxperf", type=Path, help="MaxPerf JMX-файл")
    parser.add_argument("stability", type=Path, help="Stability JMX-файл")
    parser.add_argument("-o", "--output", type=Path, default=Path("stability_changes.txt"), help="Файл отчёта")
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
