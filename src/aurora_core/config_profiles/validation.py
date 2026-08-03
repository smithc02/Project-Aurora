"""Value-safe raw and effective Aurora YAML validation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import yaml
from pydantic import ValidationError

from aurora_core.config import AuroraConfigurationError, AuroraSettings, load_settings
from aurora_core.config_profiles.models import ProfileOperationError, ProfileReason


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe loader that rejects duplicate mapping keys at every depth."""

    def compose_node(self, parent: yaml.Node | None, index: int) -> yaml.Node:
        if self.check_event(yaml.AliasEvent):  # type: ignore[no-untyped-call]
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise yaml.composer.ComposerError(
                "while composing a configuration",
                None,
                "YAML aliases are not supported",
                event.start_mark,
            )
        node = super().compose_node(parent, index)
        assert node is not None
        return node

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        keys: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in keys
            except TypeError as error:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found an unsupported mapping key",
                    key_node.start_mark,
                ) from error
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "found a duplicate mapping key",
                    key_node.start_mark,
                )
            keys.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_raw_yaml(data: bytes) -> dict[str, Any]:
    """Parse exactly one safe mapping document without disclosing its values."""
    try:
        text = data.decode("utf-8", errors="strict")
        document = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as error:
        raise ProfileOperationError(ProfileReason.INVALID_YAML) from error
    if not isinstance(document, Mapping):
        raise ProfileOperationError(ProfileReason.INVALID_YAML)
    return dict(document)


def validate_raw_yaml(
    data: bytes, *, expected_profile_id: str | None = None
) -> tuple[dict[str, Any], AuroraSettings]:
    """Validate one raw YAML mapping directly against ``AuroraSettings``."""
    document = parse_raw_yaml(data)
    try:
        settings = AuroraSettings.model_validate(document)
    except ValidationError as error:
        raise ProfileOperationError(ProfileReason.INVALID_CONFIGURATION) from error
    if (
        expected_profile_id is not None
        and settings.application.configuration_profile != expected_profile_id
    ):
        raise ProfileOperationError(ProfileReason.PROFILE_MISMATCH)
    return document, settings


def validate_effective_yaml(document: Mapping[str, Any]) -> AuroraSettings:
    """Validate through the ordinary environment-aware configuration loader."""
    try:
        return load_settings(config_data=document)
    except AuroraConfigurationError as error:
        raise ProfileOperationError(ProfileReason.INVALID_CONFIGURATION) from error
