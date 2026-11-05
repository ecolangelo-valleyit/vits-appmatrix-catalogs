"""Load WinGet YAML without executing it or turning versions into floats."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

MAX_YAML_BYTES = 1_048_576
MAX_DEPTH = 30

_RAW_STRING_KEYS = {
    "PackageVersion",
    "InstallerSha256",
    "SignatureSha256",
    "ProductCode",
    "PackageFamilyName",
    "MinimumOSVersion",
}


class YamlDocumentError(ValueError):
    pass


def load_manifest(text: str) -> dict[str, Any]:
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise YamlDocumentError("Manifest is too large.")

    loader = yaml.SafeLoader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            raise YamlDocumentError("Manifest is empty.")
        if _has_alias(node):
            raise YamlDocumentError("Manifest aliases are not supported.")
        document = _construct(loader, node, None, 0)
    except YamlDocumentError:
        raise
    except yaml.YAMLError as error:
        raise YamlDocumentError("Manifest YAML is invalid.") from error
    finally:
        loader.dispose()

    if not isinstance(document, dict):
        raise YamlDocumentError("Manifest must be a mapping.")
    return document


def _has_alias(node: Node) -> bool:
    # PyYAML 6 resolves aliases to the original node object. A repeated object
    # identity is an alias. Reject it before construction so anchors cannot expand.
    seen: set[int] = set()

    def walk(current: Node) -> bool:
        marker = id(current)
        if marker in seen:
            return True
        seen.add(marker)
        if isinstance(current, SequenceNode):
            return any(walk(child) for child in current.value)
        if isinstance(current, MappingNode):
            return any(walk(key) or walk(value) for key, value in current.value)
        return False

    return walk(node)


def _construct(loader: yaml.SafeLoader, node: Node, key: str | None, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise YamlDocumentError("Manifest is nested too deeply.")
    if isinstance(node, ScalarNode):
        if key in _RAW_STRING_KEYS:
            return str(loader.construct_scalar(node)).strip()
        value = loader.construct_object(node, deep=False)
        if isinstance(value, datetime):
            if value.hour == 0 and value.minute == 0 and value.second == 0 and value.microsecond == 0:
                return value.date().isoformat()
            return value.isoformat()
        if isinstance(value, date):
            return value.isoformat()
        return value
    if isinstance(node, SequenceNode):
        return [_construct(loader, child, None, depth + 1) for child in node.value]
    if isinstance(node, MappingNode):
        mapping: dict[str, Any] = {}
        for key_node, value_node in node.value:
            if not isinstance(key_node, ScalarNode):
                raise YamlDocumentError("Manifest keys must be strings.")
            child_key = str(loader.construct_scalar(key_node))
            mapping[child_key] = _construct(loader, value_node, child_key, depth + 1)
        return mapping
    raise YamlDocumentError("Manifest YAML is invalid.")


load_manifest.parsed_files = 0
