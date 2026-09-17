"""Tests for mirror_secrets.py's credential detection (issue #39)."""

import json

from custom_components.ha_dev_tools import mirror_secrets


def test_find_yaml_credentials_flags_literal_password():
    content = "automation:\n  - id: a\n    password: hunter2\n"
    assert mirror_secrets.find_yaml_credentials(content) == ["automation[0].password"]


def test_find_yaml_credentials_allows_secret_tag():
    content = "automation:\n  - id: a\n    password: !secret my_password\n"
    assert mirror_secrets.find_yaml_credentials(content) == []


def test_find_yaml_credentials_clean_content():
    content = "automation:\n  - id: a\n    alias: Front door\n    trigger: []\n"
    assert mirror_secrets.find_yaml_credentials(content) == []


def test_find_yaml_credentials_checks_substrings_not_just_exact_names():
    content = "sensor:\n  - platform: rest\n    api_key: abc123\n"
    assert mirror_secrets.find_yaml_credentials(content) == ["sensor[0].api_key"]


def test_find_yaml_credentials_nested_and_multiple_findings():
    content = (
        "template:\n"
        "  - sensor:\n"
        "      - name: foo\n"
        "        token: literal_token\n"
        "        options:\n"
        "          client_secret: literal_secret\n"
    )
    findings = mirror_secrets.find_yaml_credentials(content)
    assert set(findings) == {
        "template[0].sensor[0].token",
        "template[0].sensor[0].options.client_secret",
    }


def test_find_yaml_credentials_unparseable_content_is_treated_as_unsafe():
    findings = mirror_secrets.find_yaml_credentials(
        "foo: [1, 2, 3"
    )  # unclosed flow seq
    assert findings  # non-empty - refuses to call unparseable content "clean"


def test_find_storage_credentials_flags_literal_value_no_secret_exemption():
    content = json.dumps({"config_entry_id": "abc", "options": {"api_key": "literal"}})
    assert mirror_secrets.find_storage_credentials(content) == ["options.api_key"]


def test_find_storage_credentials_clean_data():
    content = json.dumps({"name": "My Sensor", "unit_of_measurement": "kWh"})
    assert mirror_secrets.find_storage_credentials(content) == []


def test_find_storage_credentials_unparseable_content_is_treated_as_unsafe():
    findings = mirror_secrets.find_storage_credentials("{not valid json")
    assert findings
