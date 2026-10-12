"""Tests for Chrome extension manifest, permissions, and zero-automation constraints."""

import json
from pathlib import Path
import re

import pytest

import config


@pytest.fixture
def manifest_data():
    manifest_path = config.PROJECT_ROOT / "extension" / "manifest.json"
    assert manifest_path.exists(), "extension/manifest.json does not exist"
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_manifest_version_and_metadata(manifest_data):
    assert manifest_data.get("manifest_version") == 3
    assert manifest_data.get("name") == "Pollinate"
    assert "version" in manifest_data


def test_manifest_permissions_are_minimal(manifest_data):
    permissions = manifest_data.get("permissions", [])
    allowed_permissions = {"contextMenus", "storage", "sidePanel"}
    assert set(permissions).issubset(allowed_permissions), (
        f"Unexpected permissions in manifest: {set(permissions) - allowed_permissions}"
    )

    # Disallowed permissions that would enable passive surveillance or notification abuse
    banned_permissions = [
        "notifications",
        "tabs",
        "activeTab",
        "scripting",
        "webRequest",
        "cookies",
        "<all_urls>",
    ]
    for banned in banned_permissions:
        assert banned not in permissions, f"Manifest must not declare '{banned}' permission"


def test_manifest_host_permissions_strictly_local(manifest_data):
    host_permissions = manifest_data.get("host_permissions", [])
    assert host_permissions == ["http://127.0.0.1/*"], (
        f"Host permissions must strictly be ['http://127.0.0.1/*'], got {host_permissions}"
    )


def test_manifest_contains_no_content_scripts(manifest_data):
    assert "content_scripts" not in manifest_data, (
        "Extension must not declare content_scripts; all captures are user-triggered via context menu"
    )


def test_extension_scripts_contain_no_dom_injection_or_automation():
    extension_dir = config.PROJECT_ROOT / "extension"
    banned_patterns = [
        re.compile(r"chrome\.scripting", re.IGNORECASE),
        re.compile(r"chrome\.tabs\.executeScript", re.IGNORECASE),
        re.compile(r"feed-shared-update", re.IGNORECASE),
        re.compile(r"urn:li:activity", re.IGNORECASE),
    ]

    for js_file in extension_dir.glob("*.js"):
        content = js_file.read_text(encoding="utf-8")
        for pattern in banned_patterns:
            assert not pattern.search(content), (
                f"{js_file.name} contains banned pattern '{pattern.pattern}'"
            )
