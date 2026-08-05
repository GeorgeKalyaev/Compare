#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Сравнение содержимого Ultimate Thread Group в двух JMX-файлах.

Особенности:
- первый файл считается MaxPerf, второй — Stability;
- профиль нагрузки самой Ultimate Thread Group НЕ сравнивается;
- сравнивается только содержимое соседнего hashTree;
- одинаковые имена Thread Group внутри одного JMX больше не вызывают ошибку;
- Thread Group сопоставляются по номерам UC в названии:
    main_UC280
    main_UC280 Отключили...
  считаются одной и той же группой UC280;
- для составных групп учитываются все номера:
    main_UC275 + UC276 + UC277 + UC278 + UC279
- проверяется состояние enabled=true/false самой Thread Group;
- показываются изменения названия/комментария в testname;
- полный XML в отчёт не выводится.

Запуск:
    python compare_jmx_thread_groups_v2.py MaxPerf.jmx Stability.jmx

С другим именем отчёта:
    python compare_jmx_thread_groups_v2.py MaxPerf.jmx Stability.jmx -o report.txt
"""

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
IGNORED_PROPERTY_NAMES: set[str] = set()


@dataclass(frozen=True)
class GroupInfo:
    original_name: str
    match_key: str
    enabled: bool
    hash_tree: ET.Element
    source_index: int


@dataclass(frozen=True)
class NodeInfo:
    path: str
    element_type: str
    test_name: str
    enabled: bool
    properties: dict[str, str]


def local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def is_hash_tree(element: ET.Element) -> bool:
    return local_tag(element.tag) == "hashTree"


def is_ultimate_thread_group(element: ET.Element) -> bool:
    values = (
        local_tag(element.tag),
        element.attrib.get("testclass", ""),
        element.attrib.get("guiclass", ""),
    )
    combined = " ".join(values).lower()
    return any(marker.lower() in combined for marker in ULTIMATE_MARKERS)


def parse_enabled(element: ET.Element) -> bool:
    return element.attrib.get("enabled", "true").strip().lower() != "false"


def enabled_text(value: bool) -> str:
    return "ВКЛЮЧЕНА" if value else "ОТКЛЮЧЕНА"


def clean_text(value: str | None) -> str:
    if value is None:
        return ""

    lines = [
        line.rstrip()
        for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines)


def shorten(value: str, limit: int = 240) -> str:
    value = value.replace("\n", "\\n")
    return value if len(value) <= limit else value[: limit - 3] + "..."


def group_match_key(test_name: str) -> str:
    """
    Формирует ключ сопоставления Thread Group.

    При наличии UC-номеров ключ строится только по ним, поэтому:
      main_UC280
      main_UC280 Отключили из-за...
    будут сопоставлены.

    Для составной группы:
      main_UC275 + UC276 + UC277
    ключ будет UC275+UC276+UC277.
    """
    uc_numbers = re.findall(r"(?i)UC[\s_-]*(\d+)", test_name)

    if uc_numbers:
        ordered_unique: list[str] = []
        seen: set[str] = set()

        for number in uc_numbers:
            normalized = str(int(number))
            if normalized not in seen:
                seen.add(normalized)
                ordered_unique.append(normalized)

        return "+".join(f"UC{number}" for number in ordered_unique)

    # Запасной вариант для групп без UC-номеров.
    normalized = test_name.casefold()
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or "(без имени)"


def element_type(element: ET.Element) -> str:
    return (
        element.attrib.get("testclass")
        or local_tag(element.tag)
        or "(неизвестный тип)"
    )


def element_name(element: ET.Element) -> str:
    return element.attrib.get("testname", "").strip() or "(без имени)"


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

    for attr_name, attr_value in sorted(element.attrib.items()):
        if attr_name in IGNORED_ATTRIBUTES:
            continue
        result[f"@{attr_name}"] = clean_text(attr_value)

    def walk(current: ET.Element, prefix: str) -> None:
        repeated: Counter[str] = Counter()

        for index, child in enumerate(list(current)):
            if is_hash_tree(child):
                continue

            base = property_label(child, index)
            repeated[base] += 1
            occurrence = repeated[base]
            label = base if occurrence == 1 else f"{base}#{occurrence}"
            path = f"{prefix}/{label}" if prefix else label

            if child.attrib.get("name", "") in IGNORED_PROPERTY_NAMES:
                continue

            text = clean_text(child.text)
            if text:
                result[f"{path}/#text"] = text

            for attr_name, attr_value in sorted(child.attrib.items()):
                if attr_name in IGNORED_ATTRIBUTES:
                    continue
                result[f"{path}/@{attr_name}"] = clean_text(attr_value)

            walk(child, path)

    walk(element, "")
    return result


def iter_jmeter_pairs(
    hash_tree: ET.Element,
) -> Iterable[tuple[ET.Element, ET.Element | None]]:
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
    inventory: dict[str, NodeInfo] = {}

    def walk(tree: ET.Element, parent_path: str) -> None:
        sibling_counts: Counter[str] = Counter()

        for element, child_tree in iter_jmeter_pairs(tree):
            node_type = element_type(element)
            node_name = element_name(element)
            base_segment = f'{node_type} "{node_name}"'

            sibling_counts[base_segment] += 1
            occurrence = sibling_counts[base_segment]
            segment = (
                base_segment
                if occurrence == 1
                else f"{base_segment} [#{occurrence}]"
            )

            path = f"{parent_path} / {segment}" if parent_path else segment

            inventory[path] = NodeInfo(
                path=path,
                element_type=node_type,
                test_name=node_name,
                enabled=parse_enabled(element),
                properties=flatten_properties(element),
            )

            if child_tree is not None:
                walk(child_tree, path)

    walk(hash_tree, "")
    return inventory


def extract_groups(jmx_path: Path) -> dict[str, list[GroupInfo]]:
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
    groups: dict[str, list[GroupInfo]] = defaultdict(list)
    source_index = 0

    for parent in root.iter():
        children = list(parent)

        for index, child in enumerate(children):
            if not is_ultimate_thread_group(child):
                continue

            original_name = element_name(child)
            key = group_match_key(original_name)

            if index + 1 < len(children) and is_hash_tree(children[index + 1]):
                contents = copy.deepcopy(children[index + 1])
            else:
                contents = ET.Element("hashTree")

            groups[key].append(
                GroupInfo(
                    original_name=original_name,
                    match_key=key,
                    enabled=parse_enabled(child),
                    hash_tree=contents,
                    source_index=source_index,
                )
            )
            source_index += 1

    return groups


def property_display_name(key: str) -> str:
    replacements = {
        "@enabled": "Состояние элемента enabled",
        "@testname": "Название элемента",
        "stringProp[HTTPSampler.path]/#text": "HTTP path",
        "stringProp[HTTPSampler.method]/#text": "HTTP method",
        "stringProp[HTTPSampler.domain]/#text": "HTTP domain",
        "stringProp[HTTPSampler.port]/#text": "HTTP port",
        "stringProp[HTTPSampler.protocol]/#text": "HTTP protocol",
        "stringProp[Argument.value]/#text": "Argument value",
        "stringProp[Argument.name]/#text": "Argument name",
        "stringProp[script]/#text": "script",
        "stringProp[filename]/#text": "filename",
    }
    return replacements.get(key, key)


def compare_properties(
    stability: NodeInfo,
    maxperf: NodeInfo,
) -> list[str]:
    lines: list[str] = []
    keys = sorted(
        set(stability.properties) | set(maxperf.properties),
        key=str.casefold,
    )

    for key in keys:
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


def inventory_signature(group: GroupInfo) -> set[str]:
    """
    Сигнатура для сопоставления повторяющихся групп с одинаковым ключом.
    Используются пути вложенных элементов без значений свойств.
    """
    return set(build_node_inventory(group.hash_tree))


def similarity(left: GroupInfo, right: GroupInfo) -> float:
    a = inventory_signature(left)
    b = inventory_signature(right)

    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0

    return len(a & b) / len(a | b)


def pair_duplicate_groups(
    maxperf_items: list[GroupInfo],
    stability_items: list[GroupInfo],
) -> tuple[
    list[tuple[GroupInfo, GroupInfo]],
    list[GroupInfo],
    list[GroupInfo],
]:
    """
    Сопоставляет повторяющиеся Thread Group одного UC-ключа по максимальному
    сходству вложенной структуры. Это устраняет ошибку из-за одинаковых имён.
    """
    remaining_max = list(maxperf_items)
    remaining_stability = list(stability_items)
    pairs: list[tuple[GroupInfo, GroupInfo]] = []

    while remaining_max and remaining_stability:
        best_score = -1.0
        best_i = 0
        best_j = 0

        for i, max_group in enumerate(remaining_max):
            for j, stability_group in enumerate(remaining_stability):
                score = similarity(max_group, stability_group)

                # При равенстве предпочитаем группы с более близкой позицией.
                position_bonus = 1.0 / (
                    1.0
                    + abs(max_group.source_index - stability_group.source_index)
                )
                combined_score = score * 1000 + position_bonus

                if combined_score > best_score:
                    best_score = combined_score
                    best_i = i
                    best_j = j

        pairs.append(
            (
                remaining_max.pop(best_i),
                remaining_stability.pop(best_j),
            )
        )

    return pairs, remaining_max, remaining_stability


def compare_group_contents(
    maxperf_group: GroupInfo,
    stability_group: GroupInfo,
) -> tuple[bool, list[str]]:
    maxperf_nodes = build_node_inventory(maxperf_group.hash_tree)
    stability_nodes = build_node_inventory(stability_group.hash_tree)

    max_paths = set(maxperf_nodes)
    stability_paths = set(stability_nodes)

    added = sorted(max_paths - stability_paths, key=str.casefold)
    removed = sorted(stability_paths - max_paths, key=str.casefold)
    common = sorted(max_paths & stability_paths, key=str.casefold)

    changed: list[tuple[str, list[str]]] = []

    for path in common:
        changes = compare_properties(
            stability_nodes[path],
            maxperf_nodes[path],
        )
        if changes:
            changed.append((path, changes))

    header_changes: list[str] = []

    if maxperf_group.original_name != stability_group.original_name:
        header_changes.extend(
            [
                "  * Отличается название Thread Group:",
                f"      Stability: {stability_group.original_name}",
                f"      MaxPerf:   {maxperf_group.original_name}",
            ]
        )

    if maxperf_group.enabled != stability_group.enabled:
        header_changes.extend(
            [
                "  * Отличается состояние Thread Group:",
                f"      Stability: {enabled_text(stability_group.enabled)}",
                f"      MaxPerf:   {enabled_text(maxperf_group.enabled)}",
            ]
        )

    has_difference = bool(header_changes or added or removed or changed)
    details: list[str] = []

    if header_changes:
        details.extend(["", "ИЗМЕНЕНИЯ САМОЙ THREAD GROUP:", *header_changes])

    if added:
        details.extend(["", f"ДОБАВЛЕНО В MAXPERF ({len(added)}):"])
        details.extend(f"  + {path}" for path in added)

    if removed:
        details.extend(
            [
                "",
                f"ЕСТЬ В STABILITY, НО НЕТ В MAXPERF ({len(removed)}):",
            ]
        )
        details.extend(f"  - {path}" for path in removed)

    if changed:
        details.extend(
            ["", f"ИЗМЕНЕНЫ НАСТРОЙКИ ЭЛЕМЕНТОВ ({len(changed)}):"]
        )
        for path, changes in changed:
            details.append(f"  * {path}")
            details.extend(changes)

    return has_difference, details


def pair_label(key: str, number: int, total: int) -> str:
    return key if total == 1 else f"{key} [экземпляр {number}]"


def compare_files(
    maxperf_path: Path,
    stability_path: Path,
    output_path: Path,
) -> int:
    maxperf_groups = extract_groups(maxperf_path)
    stability_groups = extract_groups(stability_path)

    all_keys = sorted(
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
        "ПРАВИЛА СРАВНЕНИЯ:",
        "- профиль нагрузки Ultimate Thread Group игнорируется;",
        "- вложенный hashTree сравнивается;",
        "- состояние enabled самой Thread Group сравнивается;",
        "- комментарий, дописанный в название, показывается как изменение названия;",
        "- повторяющиеся имена больше не вызывают ошибку;",
        "- повторяющиеся группы сопоставляются по сходству вложенной структуры.",
        "",
        f"Ultimate Thread Group в MaxPerf:   "
        f"{sum(len(v) for v in maxperf_groups.values())}",
        f"Ultimate Thread Group в Stability: "
        f"{sum(len(v) for v in stability_groups.values())}",
        "",
        "КРАТКИЙ РЕЗУЛЬТАТ",
        "-" * 100,
    ]

    details: list[str] = []
    identical = different = only_maxperf = only_stability = 0

    for key in all_keys:
        max_items = maxperf_groups.get(key, [])
        stability_items = stability_groups.get(key, [])

        pairs, unmatched_max, unmatched_stability = pair_duplicate_groups(
            max_items,
            stability_items,
        )

        total_pairs = len(pairs)

        for index, (max_group, stability_group) in enumerate(pairs, start=1):
            label = pair_label(key, index, total_pairs)
            has_difference, group_details = compare_group_contents(
                max_group,
                stability_group,
            )

            if has_difference:
                different += 1
                state_note = ""
                if max_group.enabled != stability_group.enabled:
                    state_note = (
                        f" — Stability: {enabled_text(stability_group.enabled)}, "
                        f"MaxPerf: {enabled_text(max_group.enabled)}"
                    )

                report.append(f"[ОТЛИЧАЕТСЯ]          {label}{state_note}")
                details.extend(
                    [
                        "",
                        "=" * 100,
                        f"THREAD GROUP: {label}",
                        f"Stability name: {stability_group.original_name}",
                        f"MaxPerf name:   {max_group.original_name}",
                        "=" * 100,
                        *group_details,
                    ]
                )
            else:
                identical += 1
                report.append(f"[ОДИНАКОВО]           {label}")

        for index, group in enumerate(unmatched_max, start=1):
            only_maxperf += 1
            label = key
            if len(unmatched_max) > 1:
                label += f" [лишний экземпляр {index}]"
            report.append(
                f"[ТОЛЬКО В MAXPERF]    {label} — "
                f"{enabled_text(group.enabled)} — {group.original_name}"
            )

        for index, group in enumerate(unmatched_stability, start=1):
            only_stability += 1
            label = key
            if len(unmatched_stability) > 1:
                label += f" [лишний экземпляр {index}]"
            report.append(
                f"[ТОЛЬКО В STABILITY]  {label} — "
                f"{enabled_text(group.enabled)} — {group.original_name}"
            )

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
        report.extend(["", "", "ПОДРОБНЫЕ ОТЛИЧИЯ", *details])

    text = "\n".join(report) + "\n"

    try:
        output_path.write_text(text, encoding="utf-8")
    except OSError as error:
        raise RuntimeError(
            f"Не удалось записать отчёт '{output_path}': {error}"
        ) from error

    print(text)
    print(f"Отчёт сохранён: {output_path.resolve()}")

    return 1 if (different or only_maxperf or only_stability) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Сравнивает содержимое Ultimate Thread Group "
            "в MaxPerf и Stability JMX."
        )
    )
    parser.add_argument("maxperf", type=Path, help="MaxPerf JMX-файл")
    parser.add_argument("stability", type=Path, help="Stability JMX-файл")
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
