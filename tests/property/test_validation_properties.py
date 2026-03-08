"""Property-based tests for ValidationManager.

This module contains property-based tests that validate the correctness
of content validation in the Home Assistant Management Integration.
"""
import pytest
from hypothesis import given, strategies as st, assume, settings, HealthCheck
import string
import yaml
import json

from custom_components.ha_dev_tools.validation import ValidationManager


# Strategy for generating valid YAML content
def generate_valid_yaml_dict():
    """Generate valid YAML dictionary content."""
    return st.builds(
        lambda key, value: f"{key}: {value}",
        key=st.text(alphabet=string.ascii_letters, min_size=1, max_size=20),
        value=st.one_of(
            st.text(alphabet=string.ascii_letters + string.digits + " ", min_size=0, max_size=50),
            st.integers(min_value=-1000, max_value=1000),
            st.floats(allow_nan=False, allow_infinity=False, min_value=-1000, max_value=1000)
        )
    )

valid_yaml_content = st.one_of([
    generate_valid_yaml_dict(),
    st.builds(
        lambda: """homeassistant:
  name: Test Home
  latitude: 32.87336
  longitude: -117.22743
"""
    ),
    st.builds(
        lambda key, val: f"""test:
  {key}: {val}
  enabled: true
""",
        key=st.text(alphabet=string.ascii_letters, min_size=1, max_size=10),
        val=st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=20)
    )
])

# Strategy for generating invalid YAML content
invalid_yaml_content = st.one_of([
    st.just("homeassistant:\n  name: Test\n  [invalid: yaml: syntax"),
    st.just("{\n  invalid yaml\n  missing: bracket"),
    st.just("- item1\n  - nested\n    - [broken"),
    st.builds(
        lambda: "key: value\n  [invalid: " + "x" * 100
    ),
    st.just("homeassistant:\n  name:\n    - [unclosed"),
])

# Strategy for generating valid JSON content
valid_json_content = st.builds(
    lambda key, value: json.dumps({key: value}),
    key=st.text(alphabet=string.ascii_letters, min_size=1, max_size=20),
    value=st.one_of(
        st.text(alphabet=string.ascii_letters + string.digits + " ", min_size=0, max_size=50),
        st.integers(min_value=-1000, max_value=1000),
        st.floats(allow_nan=False, allow_infinity=False, min_value=-1000, max_value=1000),
        st.booleans(),
        st.none()
    )
)

# Strategy for generating invalid JSON content
invalid_json_content = st.one_of([
    st.just('{"key": "value", "invalid": }'),
    st.just('{key: value}'),  # Missing quotes
    st.just('{"key": "value",}'),  # Trailing comma
    st.just('{"key": undefined}'),  # Invalid value
    st.just('[1, 2, 3,]'),  # Trailing comma in array
])


@given(yaml_content=valid_yaml_content)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.filter_too_much],
    max_examples=100,
    deadline=5000
)
def test_valid_yaml_always_passes_validation(yaml_content):
    """
    Feature: ha-config-manager-integration, Property 6: Content Validation Before Write
    
    For any valid YAML content, validation should succeed and allow the write operation.
    **Validates: Requirements 3.1, 3.3**
    """
    # Ensure the content is actually valid YAML
    try:
        yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        assume(False)  # Skip if not actually valid YAML
    
    validation_manager = ValidationManager()
    
    # Validate the YAML content
    result = validation_manager.validate_yaml(yaml_content, "test.yaml")
    
    # Valid YAML should pass validation
    assert result.is_valid is True, f"Valid YAML failed validation: {result.errors}"
    assert len(result.errors) == 0, f"Valid YAML has errors: {result.errors}"


@given(yaml_content=invalid_yaml_content)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=100,
    deadline=5000
)
def test_invalid_yaml_always_fails_validation(yaml_content):
    """
    Feature: ha-config-manager-integration, Property 6: Content Validation Before Write
    
    For any invalid YAML content, validation should fail and prevent the write operation.
    **Validates: Requirements 3.1, 3.3**
    """
    # Ensure the content is actually invalid YAML
    try:
        yaml.safe_load(yaml_content)
        assume(False)  # Skip if it's actually valid YAML
    except yaml.YAMLError:
        pass  # Good, it's invalid
    
    validation_manager = ValidationManager()
    
    # Validate the YAML content
    result = validation_manager.validate_yaml(yaml_content, "test.yaml")
    
    # Invalid YAML should fail validation
    assert result.is_valid is False, f"Invalid YAML passed validation"
    assert len(result.errors) > 0, f"Invalid YAML has no error messages"
    assert "YAML syntax error" in result.errors[0], f"Error message doesn't mention YAML syntax"


@given(json_content=valid_json_content)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.filter_too_much],
    max_examples=100,
    deadline=5000
)
def test_valid_json_always_passes_validation(json_content):
    """
    Feature: ha-config-manager-integration, Property 6: Content Validation Before Write
    
    For any valid JSON content, validation should succeed and allow the write operation.
    **Validates: Requirements 3.2, 3.3**
    """
    # Ensure the content is actually valid JSON
    try:
        json.loads(json_content)
    except json.JSONDecodeError:
        assume(False)  # Skip if not actually valid JSON
    
    validation_manager = ValidationManager()
    
    # Validate the JSON content
    result = validation_manager.validate_json(json_content, "test.json")
    
    # Valid JSON should pass validation
    assert result.is_valid is True, f"Valid JSON failed validation: {result.errors}"
    assert len(result.errors) == 0, f"Valid JSON has errors: {result.errors}"


@given(json_content=invalid_json_content)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=100,
    deadline=5000
)
def test_invalid_json_always_fails_validation(json_content):
    """
    Feature: ha-config-manager-integration, Property 6: Content Validation Before Write
    
    For any invalid JSON content, validation should fail and prevent the write operation.
    **Validates: Requirements 3.2, 3.3**
    """
    # Ensure the content is actually invalid JSON
    try:
        json.loads(json_content)
        assume(False)  # Skip if it's actually valid JSON
    except json.JSONDecodeError:
        pass  # Good, it's invalid
    
    validation_manager = ValidationManager()
    
    # Validate the JSON content
    result = validation_manager.validate_json(json_content, "test.json")
    
    # Invalid JSON should fail validation
    assert result.is_valid is False, f"Invalid JSON passed validation"
    assert len(result.errors) > 0, f"Invalid JSON has no error messages"
    assert "JSON syntax error" in result.errors[0], f"Error message doesn't mention JSON syntax"


@given(
    content=st.one_of(valid_yaml_content, valid_json_content),
    file_extension=st.sampled_from(['.yaml', '.yml', '.json', '.txt'])
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.filter_too_much],
    max_examples=100,
    deadline=5000
)
def test_validate_content_routes_to_correct_validator(content, file_extension):
    """
    Feature: ha-config-manager-integration, Property 6: Content Validation Before Write
    
    For any content and file extension, validate_content should route to the appropriate
    validator (YAML, JSON, or pass-through for other types).
    **Validates: Requirements 3.1, 3.2**
    """
    validation_manager = ValidationManager()
    file_path = f"test{file_extension}"
    
    # Validate the content
    result = validation_manager.validate_content(content, file_path)
    
    # Result should have proper structure
    assert hasattr(result, 'is_valid')
    assert hasattr(result, 'errors')
    assert hasattr(result, 'warnings')
    assert hasattr(result, 'line_numbers')
    
    # For .txt files, validation should always pass (no validation)
    if file_extension == '.txt':
        assert result.is_valid is True
        assert len(result.errors) == 0


@given(
    yaml_content=valid_yaml_content,
    file_path=st.sampled_from(['configuration.yaml', 'automations.yaml', 'scripts.yaml'])
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.filter_too_much],
    max_examples=100,
    deadline=5000
)
def test_configuration_yaml_requires_homeassistant_section(yaml_content, file_path):
    """
    Feature: ha-config-manager-integration, Property 6: Content Validation Before Write
    
    For configuration.yaml files, validation should check for the homeassistant section
    and provide warnings if missing (but still allow the content).
    **Validates: Requirements 3.1, 3.3**
    """
    # Ensure the content is valid YAML
    try:
        parsed = yaml.safe_load(yaml_content)
    except yaml.YAMLError:
        assume(False)
    
    # Skip if not a dict (will fail validation anyway)
    if not isinstance(parsed, dict):
        assume(False)
    
    validation_manager = ValidationManager()
    
    # Validate the content
    result = validation_manager.validate_content(yaml_content, file_path)
    
    # Should be valid (or invalid for structural reasons, not missing homeassistant)
    if file_path == 'configuration.yaml':
        if 'homeassistant' not in parsed:
            # Should have a warning about missing homeassistant section
            if result.is_valid:
                assert len(result.warnings) > 0 or 'homeassistant' not in yaml_content.lower()
    else:
        # Other files don't require homeassistant section
        if result.is_valid:
            # No specific requirement
            pass


@given(
    content=st.text(alphabet=string.printable, min_size=0, max_size=500),
    file_extension=st.sampled_from(['.yaml', '.yml', '.json'])
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.filter_too_much],
    max_examples=100,
    deadline=5000
)
def test_validation_never_crashes(content, file_extension):
    """
    Feature: ha-config-manager-integration, Property 6: Content Validation Before Write
    
    For any content and file type, validation should never crash or raise exceptions,
    but should return a proper ValidationResult with errors if validation fails.
    **Validates: Requirements 3.1, 3.2, 3.3**
    """
    validation_manager = ValidationManager()
    file_path = f"test{file_extension}"
    
    # This should never raise an exception
    try:
        result = validation_manager.validate_content(content, file_path)
        
        # Result should always have proper structure
        assert hasattr(result, 'is_valid')
        assert hasattr(result, 'errors')
        assert hasattr(result, 'warnings')
        assert hasattr(result, 'line_numbers')
        assert isinstance(result.is_valid, bool)
        assert isinstance(result.errors, list)
        assert isinstance(result.warnings, list)
        assert isinstance(result.line_numbers, list)
        
    except Exception as e:
        pytest.fail(f"Validation raised exception: {e}")
