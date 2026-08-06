#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import copy
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path


ULTIMATE_MARKERS = (
    "kg.apc.jmeter.threads.UltimateThreadGroup",
    "UltimateThreadGroup",
    "Ultimate Thread Group",
)


@dataclass
class GroupRef:
    key: str
    name: str
    enabled: bool
    element: ET.Element
    subtree: ET.Element
    parent: ET.Element
    element_index: int


def local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def is_hash_tree(element: ET.Element) -> bool:
    return local_tag(element.tag) == "hashTree"


def is_ultimate_group(element: ET.Element) -> bool:
    values = (
        local_tag(element.tag),
        element.attrib.get("testclass", ""),
        element.attrib.get("guiclass", ""),
    )
    combined = " ".join(values).lower()
    return any(marker.lower() in combined for marker in ULTIMATE_MARKERS)


def is_enabled(element: ET.Element) -> bool:
    return element.attrib.get("enabled", "true").strip().lower() != "false"


def element_name(element: ET.Element) -> str:
    return element.attrib.get("testname", "").strip() or "(без имени)"


def uc_key(name: str) -> str:
    numbers = re.findall(r"(?i)UC[\s_-]*(\d+)", name)
    if numbers:
        result = []
        seen = set()
        for number in numbers:
            key = f"UC{int(number)}"
            if key not in seen:
                seen.add(key)
                result.append(key)
        return "+".join(result)

    normalized = re.sub(r"\s+", " ", name.casefold()).strip()
    return normalized or "(без имени)"


def normalize_requested_key(value: str) -> str:
    match = re.fullmatch(r"(?i)(?:UC)?[\s_-]*(\d+)", value.strip())
    if match:
        return f"UC{int(match.group(1))}"
    return uc_key(value)


def parse_jmx(path: Path) -> ET.ElementTree:
    try:
        return ET.parse(path)
    except ET.ParseError as error:
        raise RuntimeError(f"Не удалось разобрать XML '{path}': {error}") from error
    except OSError as error:
        raise RuntimeError(f"Не удалось открыть '{path}': {error}") from error


def collect_groups(root: ET.Element) -> list[GroupRef]:
    groups = []

    for parent in root.iter():
        children = list(parent)

        for index, child in enumerate(children):
            if not is_ultimate_group(child):
                continue

            if index + 1 < len(children) and is_hash_tree(children[index + 1]):
                subtree = children[index + 1]
            else:
                subtree = ET.Element("hashTree")

            name = element_name(child)

            groups.append(
                GroupRef(
                    key=uc_key(name),
                    name=name,
                    enabled=is_enabled(child),
                    element=child,
                    subtree=subtree,
                    parent=parent,
                    element_index=index,
                )
            )

    return groups


def find_template(stability_groups: list[GroupRef], requested_key: str) -> GroupRef:
    matches = [group for group in stability_groups if group.key == requested_key]

    if not matches:
        available = ", ".join(sorted({group.key for group in stability_groups}))
        raise RuntimeError(
            f"Шаблон '{requested_key}' не найден в Stability. "
            f"Доступные ключи: {available}"
        )

    matches.sort(key=lambda group: (not group.enabled, group.element_index))
    return matches[0]


def build_stability_group_element(
    template: GroupRef,
    maxperf_group: GroupRef,
) -> ET.Element:
    """
    Берёт Ultimate Thread Group из Stability как шаблон профиля нагрузки,
    но подставляет имя копируемой группы из MaxPerf.
    """
    result = copy.deepcopy(template.element)
    result.attrib["testname"] = maxperf_group.name
    result.attrib["enabled"] = "true"

    for child in result.iter():
        if child.attrib.get("name") == "TestPlan.comments":
            child.text = ""

    return result


def indent_xml(element: ET.Element, level: int = 0) -> None:
    indentation = "\n" + "  " * level

    if len(element):
        if not element.text or not element.text.strip():
            element.text = indentation + "  "

        for child in element:
            indent_xml(child, level + 1)

        if not element[-1].tail or not element[-1].tail.strip():
            element[-1].tail = indentation

    if level and (not element.tail or not element.tail.strip()):
        element.tail = indentation


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Копирует отсутствующие активные Ultimate Thread Group "
            "из MaxPerf в Stability и применяет профиль нагрузки "
            "из выбранной Stability-группы."
        )
    )

    parser.add_argument("maxperf", type=Path, help="MaxPerf JMX")
    parser.add_argument("stability", type=Path, help="Stability JMX")

    parser.add_argument(
        "--template",
        required=True,
        help="UC-группа из Stability, чей профиль использовать, например UC475",
    )

    parser.add_argument(
        "--only",
        action="append",
        default=[],
        help="Перенести только указанную UC-группу; параметр можно повторять",
    )

    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("Stability_with_missing_groups.jmx"),
        help="Новый JMX-файл",
    )

    args = parser.parse_args()

    if not args.maxperf.is_file():
        print(f"Ошибка: MaxPerf-файл не найден: {args.maxperf}", file=sys.stderr)
        return 2

    if not args.stability.is_file():
        print(f"Ошибка: Stability-файл не найден: {args.stability}", file=sys.stderr)
        return 2

    try:
        maxperf_tree = parse_jmx(args.maxperf)
        stability_tree = parse_jmx(args.stability)

        maxperf_groups = collect_groups(maxperf_tree.getroot())
        stability_groups = collect_groups(stability_tree.getroot())

        template_key = normalize_requested_key(args.template)
        template = find_template(stability_groups, template_key)

        stability_keys = {group.key for group in stability_groups}
        only_keys = {normalize_requested_key(value) for value in args.only}

        candidates = [
            group
            for group in maxperf_groups
            if group.enabled
            and group.key not in stability_keys
            and (not only_keys or group.key in only_keys)
        ]

        if not candidates:
            print("Нет групп для переноса.")
            return 0

        target_parent = template.parent

        print("Будут перенесены:")
        for group in candidates:
            print(f"  - {group.key}: {group.name}")

        for group in candidates:
            new_group = build_stability_group_element(template, group)
            new_subtree = copy.deepcopy(group.subtree)

            target_parent.append(new_group)
            target_parent.append(new_subtree)

        indent_xml(stability_tree.getroot())

        stability_tree.write(
            args.output,
            encoding="UTF-8",
            xml_declaration=True,
        )

        print()
        print(f"Готово. Перенесено групп: {len(candidates)}")
        print(f"Результат: {args.output.resolve()}")
        print("Исходный Stability-файл не изменялся.")

        return 0

    except RuntimeError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
