#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Сравнение содержимого Ultimate Thread Group в двух JMX-файлах.

Что делает скрипт:
- принимает два JMX-файла: MaxPerf и Stability;
- находит Ultimate Thread Group по уникальному имени testname;
- НЕ сравнивает профиль нагрузки самой Ultimate Thread Group;
- сравнивает только элементы, вложенные в её соседний hashTree;
- показывает, в какой Thread Group что добавлено, удалено или изменено;
- не печатает полный XML;
- сохраняет подробный текстовый отчёт.

Запуск:
    python compare_jmx_thread_groups.py MaxPerf.jmx Stability.jmx

С собственным именем отчёта:
    python compare_jmx_thread_groups.py MaxPerf.jmx Stability.jmx -o report.txt
"""

from __future__ import annotations

import argparse
import copy
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

# Атрибуты интерфейса JMeter обычно не влияют на логику теста.
IGNORED_ATTRIBUTES = {"guiclass"}

# Технические свойства, которые часто создают шум и обычно не влияют
# на смысл сравнения. При необходимости их можно убрать из списка.
IGNORED_PROPERTY_NAMES = set()


@dataclass(frozen=True)
class NodeInfo:
    """Описание одного элемента внутри Thread Group."""
    path: str
    element_type: str
    test_name: str
    properties: dict[str, str]


def local_tag(tag: str) -> str:
    """Убирает XML namespace, если он присутствует."""
    return tag.rsplit("}", 1)[-1]


def is_hash_tree(element: ET.Element) -> bool:
    return local_tag(element.tag) == "hashTree"


def is_ultimate_thread_group(element: ET.Element) -> bool:
    """Проверяет, является ли XML-элемент Ultimate Thread Group."""
    values = (
        local_tag(element.tag),
        element.attrib.get("testclass", ""),
        element.attrib.get("guiclass", ""),
    )
    combined = " ".join(values).lower()
    return any(marker.lower() in combined for marker in ULTIMATE_MARKERS)


def element_type(element: ET.Element) -> str:
    """Возвращает понятный тип JMeter-элемента."""
    return (
        element.attrib.get("testclass")
        or local_tag(element.tag)
        or "(неизвестный тип)"
    )


def element_name(element: ET.Element) -> str:
    """Возвращает testname либо запасное имя."""
    return element.attrib.get("testname", "").strip() or "(без имени)"


def clean_text(value: str | None) -> str:
    """Нормализует переносы строк и пробелы."""
    if value is None:
        return ""

    lines = [line.rstrip() for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")]

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines)


def shorten(value: str, limit: int = 240) -> str:
    """Сокращает слишком длинные значения в отчёте."""
    value = value.replace("\n", "\\n")
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def property_label(element: ET.Element, index: int) -> str:
    """Строит устойчивую подпись XML-свойства."""
    tag = local_tag(element.tag)
    name = (
        element.attrib.get("name")
        or element.attrib.get("elementType")
        or element.attrib.get("testname")
        or str(index)
    )
    return f"{tag}[{name}]"


def flatten_properties(element: ET.Element) -> dict[str, str]:
    """
    Преобразует настройки одного JMeter-элемента в словарь.

    Вложенные hashTree сюда не входят: они обходятся отдельно как дочерние
    JMeter-элементы. Благодаря этому отчёт показывает смысловые изменения,
    а не полный XML.
    """
    result: dict[str, str] = {}

    for attr_name, attr_value in sorted(element.attrib.items()):
        if attr_name in IGNORED_ATTRIBUTES:
            continue
        result[f"@{attr_name}"] = clean_text(attr_value)

    def walk(current: ET.Element, prefix: str) -> None:
        same_labels: Counter[str] = Counter()

        for index, child in enumerate(list(current)):
            if is_hash_tree(child):
                continue

            base_label = property_label(child, index)
            same_labels[base_label] += 1
            occurrence = same_labels[base_label]
            label = base_label if occurrence == 1 else f"{base_label}#{occurrence}"
            child_path = f"{prefix}/{label}" if prefix else label

            property_name = child.attrib.get("name", "")
            if property_name in IGNORED_PROPERTY_NAMES:
                continue

            text = clean_text(child.text)
            if text:
                result[f"{child_path}/#text"] = text

            for attr_name, attr_value in sorted(child.attrib.items()):
                if attr_name in IGNORED_ATTRIBUTES:
                    continue
                result[f"{child_path}/@{attr_name}"] = clean_text(attr_value)

            walk(child, child_path)

    walk(element, "")
    return result


def iter_jmeter_pairs(hash_tree: ET.Element) -> Iterable[tuple[ET.Element, ET.Element | None]]:
    """
    Итерирует JMeter-структуру вида:
        testElement
        hashTree
        testElement
        hashTree
    """
    children = list(hash_tree)
    index = 0

    while index < len(children):
        current = children[index]

        if is_hash_tree(current):
            index += 1
            continue

        child_tree: ET.Element | None = None
        if index + 1 < len(children) and is_hash_tree(children[index + 1]):
            child_tree = children[index + 1]
            index += 2
        else:
            index += 1

        yield current, child_tree


def build_node_inventory(hash_tree: ET.Element) -> dict[str, NodeInfo]:
    """
    Строит словарь всех элементов внутри Thread Group.

    Ключ — структурный путь:
        Transaction Controller "Create"
        / HTTP Request "POST /unit"
        / JSON Extractor "unitId"
    """
    inventory: dict[str, NodeInfo] = {}

    def walk(tree: ET.Element, parent_path: str) -> None:
        sibling_counts: Counter[str] = Counter()

        for element, child_tree in iter_jmeter_pairs(tree):
            node_type = element_type(element)
            node_name = element_name(element)
            base_segment = f'{node_type} "{node_name}"'

            sibling_counts[base_segment] += 1
            occurrence = sibling_counts[base_segment]
            segment = base_segment if occurrence == 1 else f"{base_segment} [#{occurrence}]"

            path = f"{parent_path} / {segment}" if parent_path else segment

            inventory[path] = NodeInfo(
                path=path,
                element_type=node_type,
                test_name=node_name,
                properties=flatten_properties(element),
            )

            if child_tree is not None:
                walk(child_tree, path)

    walk(hash_tree, "")
    return inventory


def extract_groups(jmx_path: Path) -> dict[str, ET.Element]:
    """
    Извлекает соседний hashTree каждой Ultimate Thread Group.

    Сам элемент Ultimate Thread Group и его профиль нагрузки не сравниваются.
    Имена групп должны быть уникальными.
    """
    try:
        tree = ET.parse(jmx_path)
    except ET.ParseError as error:
        raise RuntimeError(
            f"Не удалось разобрать XML в файле '{jmx_path}': {error}"
        ) from error
    except OSError as error:
        raise RuntimeError(
            f"Не удалось открыть файл '{jmx_path}': {error}"
        ) from error

    root = tree.getroot()
    groups: dict[str, ET.Element] = {}
    duplicates: defaultdict[str, int] = defaultdict(int)

    for parent in root.iter():
        children = list(parent)

        for index, child in enumerate(children):
            if not is_ultimate_thread_group(child):
                continue

            name = element_name(child)
            duplicates[name] += 1

            if duplicates[name] > 1:
                raise RuntimeError(
                    f"В файле '{jmx_path}' найдено несколько Ultimate Thread Group "
                    f"с одинаковым именем '{name}'. Имена должны быть уникальными."
                )

            if index + 1 < len(children) and is_hash_tree(children[index + 1]):
                groups[name] = copy.deepcopy(children[index + 1])
            else:
                groups[name] = ET.Element("hashTree")

    return groups


def property_display_name(key: str) -> str:
    """Делает технический путь свойства немного короче."""
    replacements = {
        "stringProp[HTTPSampler.path]/#text": "HTTP path",
        "stringProp[HTTPSampler.method]/#text": "HTTP method",
        "stringProp[HTTPSampler.domain]/#text": "HTTP domain",
        "stringProp[HTTPSampler.port]/#text": "HTTP port",
        "stringProp[HTTPSampler.protocol]/#text": "HTTP protocol",
        "stringProp[Argument.value]/#text": "Argument value",
        "stringProp[Argument.name]/#text": "Argument name",
        "stringProp[script]/#text": "script",
        "stringProp[filename]/#text": "filename",
        "boolProp[enabled]/#text": "enabled",
    }
    return replacements.get(key, key)


def compare_properties(
    stability: NodeInfo,
    maxperf: NodeInfo,
) -> list[str]:
    """Возвращает человекочитаемый список изменений настроек элемента."""
    lines: list[str] = []
    all_keys = sorted(
        set(stability.properties) | set(maxperf.properties),
        key=str.casefold,
    )

    for key in all_keys:
        old = stability.properties.get(key)
        new = maxperf.properties.get(key)
        label = property_display_name(key)

        if old is None:
            lines.append(f"      + свойство {label}: {shorten(new or '')}")
        elif new is None:
            lines.append(f"      - свойство {label}: {shorten(old)}")
        elif old != new:
            lines.append(f"      * {label}")
            lines.append(f"          Stability: {shorten(old)}")
            lines.append(f"          MaxPerf:   {shorten(new)}")

    return lines


def compare_group(
    group_name: str,
    maxperf_tree: ET.Element,
    stability_tree: ET.Element,
) -> tuple[bool, list[str]]:
    """Сравнивает содержимое одной Thread Group."""
    maxperf_nodes = build_node_inventory(maxperf_tree)
    stability_nodes = build_node_inventory(stability_tree)

    maxperf_paths = set(maxperf_nodes)
    stability_paths = set(stability_nodes)

    added = sorted(maxperf_paths - stability_paths, key=str.casefold)
    removed = sorted(stability_paths - maxperf_paths, key=str.casefold)
    common = sorted(maxperf_paths & stability_paths, key=str.casefold)

    changed: list[tuple[str, list[str]]] = []

    for path in common:
        property_changes = compare_properties(
            stability_nodes[path],
            maxperf_nodes[path],
        )
        if property_changes:
            changed.append((path, property_changes))

    has_difference = bool(added or removed or changed)
    lines: list[str] = []

    if not has_difference:
        return False, lines

    lines.extend(
        [
            "",
            "=" * 100,
            f"THREAD GROUP: {group_name}",
            "=" * 100,
        ]
    )

    if added:
        lines.append("")
        lines.append(f"ДОБАВЛЕНО В MAXPERF ({len(added)}):")
        for path in added:
            lines.append(f"  + {path}")

    if removed:
        lines.append("")
        lines.append(f"ОТСУТСТВУЕТ В MAXPERF / ЕСТЬ В STABILITY ({len(removed)}):")
        for path in removed:
            lines.append(f"  - {path}")

    if changed:
        lines.append("")
        lines.append(f"ИЗМЕНЕНЫ НАСТРОЙКИ ЭЛЕМЕНТОВ ({len(changed)}):")
        for path, property_changes in changed:
            lines.append(f"  * {path}")
            lines.extend(property_changes)

    return True, lines


def compare_files(
    maxperf_path: Path,
    stability_path: Path,
    output_path: Path,
) -> int:
    maxperf_groups = extract_groups(maxperf_path)
    stability_groups = extract_groups(stability_path)

    all_group_names = sorted(
        set(maxperf_groups) | set(stability_groups),
        key=str.casefold,
    )

    report: list[str] = [
        "=" * 100,
        "СРАВНЕНИЕ СОДЕРЖИМОГО ULTIMATE THREAD GROUP",
        "=" * 100,
        f"MaxPerf:   {maxperf_path}",
        f"Stability: {stability_path}",
        "",
        "ВАЖНО:",
        "- профиль нагрузки самой Ultimate Thread Group не сравнивается;",
        "- сравнивается только содержимое вложенного hashTree;",
        "- строки Stability показывают старое значение;",
        "- строки MaxPerf показывают новое значение.",
        "",
        f"Ultimate Thread Group в MaxPerf:   {len(maxperf_groups)}",
        f"Ultimate Thread Group в Stability: {len(stability_groups)}",
        "",
        "КРАТКИЙ РЕЗУЛЬТАТ",
        "-" * 100,
    ]

    identical = 0
    different = 0
    only_maxperf = 0
    only_stability = 0
    details: list[str] = []

    for group_name in all_group_names:
        maxperf_tree = maxperf_groups.get(group_name)
        stability_tree = stability_groups.get(group_name)

        if maxperf_tree is None:
            only_stability += 1
            report.append(f"[ТОЛЬКО В STABILITY] {group_name}")
            continue

        if stability_tree is None:
            only_maxperf += 1
            report.append(f"[ТОЛЬКО В MAXPERF]   {group_name}")
            continue

        has_difference, group_details = compare_group(
            group_name,
            maxperf_tree,
            stability_tree,
        )

        if has_difference:
            different += 1
            report.append(f"[ОТЛИЧАЕТСЯ]          {group_name}")
            details.extend(group_details)
        else:
            identical += 1
            report.append(f"[ОДИНАКОВО]           {group_name}")

    report.extend(
        [
            "",
            "ИТОГО",
            "-" * 100,
            f"Одинаковые:         {identical}",
            f"Отличающиеся:       {different}",
            f"Только в MaxPerf:   {only_maxperf}",
            f"Только в Stability: {only_stability}",
        ]
    )

    if details:
        report.extend(
            [
                "",
                "",
                "ПОДРОБНЫЕ ОТЛИЧИЯ",
                *details,
            ]
        )

    report_text = "\n".join(report) + "\n"

    try:
        output_path.write_text(report_text, encoding="utf-8")
    except OSError as error:
        raise RuntimeError(
            f"Не удалось записать отчёт '{output_path}': {error}"
        ) from error

    print(report_text)
    print(f"Отчёт сохранён: {output_path.resolve()}")

    return 1 if (different or only_maxperf or only_stability) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Сравнивает содержимое Ultimate Thread Group в MaxPerf и Stability JMX."
        )
    )
    parser.add_argument(
        "maxperf",
        type=Path,
        help="Путь к MaxPerf JMX-файлу",
    )
    parser.add_argument(
        "stability",
        type=Path,
        help="Путь к Stability JMX-файлу",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("thread_groups_diff.txt"),
        help="Файл отчёта (по умолчанию thread_groups_diff.txt)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if not args.maxperf.is_file():
        print(
            f"Ошибка: MaxPerf-файл не найден: {args.maxperf}",
            file=sys.stderr,
        )
        return 2

    if not args.stability.is_file():
        print(
            f"Ошибка: Stability-файл не найден: {args.stability}",
            file=sys.stderr,
        )
        return 2

    try:
        return compare_files(
            maxperf_path=args.maxperf,
            stability_path=args.stability,
            output_path=args.output,
        )
    except RuntimeError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
