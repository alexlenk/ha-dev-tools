"""Unit tests for the ValidationManager."""
import pytest

from custom_components.ha_config_manager.validation import ValidationManager, ValidationResult


class TestValidationManager:
    """Test the ValidationManager class."""

    @pytest.fixture
    def validation_manager(self):
        """Create a ValidationManager instance for testing."""
        return ValidationManager()

    def test_validate_yaml_valid_configuration(self, validation_manager):
        """Test YAML validation with valid configuration.yaml content."""
        valid_yaml = """homeassistant:
  name: Test Home
  latitude: 32.87336
  longitude: -117.22743
  elevation: 0
  unit_system: metric
  time_zone: America/Los_Angeles
"""
        result = validation_manager.validate_yaml(valid_yaml, "configuration.yaml")
        
        assert result.is_valid is True
        assert len(result.errors) == 0
        assert len(result.warnings) == 0

    def test_validate_yaml_invalid_syntax(self, validation_manager):
        """Test YAML validation with invalid syntax."""
        invalid_yaml = """homeassistant:
  name: Test Home
  [invalid: yaml: syntax
"""
        result = validation_manager.validate_yaml(invalid_yaml, "configuration.yaml")
        
        assert result.is_valid is False
        assert len(result.errors) > 0
        assert "YAML syntax error" in result.errors[0]
        assert len(result.line_numbers) > 0

    def test_validate_yaml_missing_homeassistant_section(self, validation_manager):
        """Test YAML validation with missing homeassistant section."""
        yaml_without_ha = """automation:
  - alias: Test
    trigger:
      platform: state
"""
        result = validation_manager.validate_yaml(yaml_without_ha, "configuration.yaml")
        
        # Should be valid but with a warning
        assert result.is_valid is True
        assert len(result.warnings) > 0
        assert "homeassistant" in result.warnings[0].lower()

    def test_validate_yaml_not_dict(self, validation_manager):
        """Test YAML validation when content is not a dictionary."""
        yaml_list = """- item1
- item2
- item3
"""
        result = validation_manager.validate_yaml(yaml_list, "configuration.yaml")
        
        assert result.is_valid is False
        assert len(result.errors) > 0
        assert "dictionary" in result.errors[0].lower()

    def test_validate_yaml_empty_content(self, validation_manager):
        """Test YAML validation with empty content."""
        empty_yaml = ""
        result = validation_manager.validate_yaml(empty_yaml, "configuration.yaml")
        
        # Empty YAML is valid (parses to None)
        assert result.is_valid is False
        assert len(result.errors) > 0

    def test_validate_yaml_non_configuration_file(self, validation_manager):
        """Test YAML validation for non-configuration.yaml files."""
        valid_yaml = """- alias: Test Automation
  trigger:
    platform: state
    entity_id: light.living_room
"""
        result = validation_manager.validate_yaml(valid_yaml, "automations.yaml")
        
        # Should be valid, no homeassistant section required
        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_validate_json_valid(self, validation_manager):
        """Test JSON validation with valid content."""
        valid_json = '{"key": "value", "number": 42, "array": [1, 2, 3]}'
        result = validation_manager.validate_json(valid_json, "test.json")
        
        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_validate_json_invalid_syntax(self, validation_manager):
        """Test JSON validation with invalid syntax."""
        invalid_json = '{"key": "value", "invalid": }'
        result = validation_manager.validate_json(invalid_json, "test.json")
        
        assert result.is_valid is False
        assert len(result.errors) > 0
        assert "JSON syntax error" in result.errors[0]

    def test_validate_json_malformed(self, validation_manager):
        """Test JSON validation with malformed content."""
        malformed_json = '{key: value}'  # Missing quotes
        result = validation_manager.validate_json(malformed_json, "test.json")
        
        assert result.is_valid is False
        assert len(result.errors) > 0

    def test_validate_content_yaml_file(self, validation_manager):
        """Test validate_content with YAML file extension."""
        valid_yaml = """homeassistant:
  name: Test
"""
        result = validation_manager.validate_content(valid_yaml, "configuration.yaml")
        
        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_validate_content_yml_file(self, validation_manager):
        """Test validate_content with .yml file extension."""
        valid_yaml = """test:
  key: value
"""
        result = validation_manager.validate_content(valid_yaml, "test.yml")
        
        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_validate_content_json_file(self, validation_manager):
        """Test validate_content with JSON file extension."""
        valid_json = '{"test": "value"}'
        result = validation_manager.validate_content(valid_json, "test.json")
        
        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_validate_content_other_file_type(self, validation_manager):
        """Test validate_content with non-YAML/JSON file."""
        text_content = "This is plain text content"
        result = validation_manager.validate_content(text_content, "test.txt")
        
        # Other file types should pass validation
        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_validate_storage_file_valid(self, validation_manager):
        """Test storage file validation with valid content."""
        valid_storage = '{"version": 1, "data": {"key": "value"}}'
        result = validation_manager.validate_storage_file(valid_storage, "entity_registry")
        
        assert result.is_valid is True
        assert len(result.errors) == 0

    def test_validate_storage_file_missing_version(self, validation_manager):
        """Test storage file validation with missing version."""
        storage_no_version = '{"data": {"key": "value"}}'
        result = validation_manager.validate_storage_file(storage_no_version, "entity_registry")
        
        # Should be valid but with warning
        assert result.is_valid is True
        assert len(result.warnings) > 0
        assert "version" in result.warnings[0].lower()

    def test_validate_storage_file_not_dict(self, validation_manager):
        """Test storage file validation when content is not a dict."""
        storage_array = '[1, 2, 3]'
        result = validation_manager.validate_storage_file(storage_array, "entity_registry")
        
        assert result.is_valid is False
        assert len(result.errors) > 0
        assert "object" in result.errors[0].lower()

    def test_validate_storage_file_invalid_json(self, validation_manager):
        """Test storage file validation with invalid JSON."""
        invalid_storage = '{"version": 1, invalid}'
        result = validation_manager.validate_storage_file(invalid_storage, "entity_registry")
        
        assert result.is_valid is False
        assert len(result.errors) > 0

    def test_validation_result_structure(self, validation_manager):
        """Test that ValidationResult has correct structure."""
        result = validation_manager.validate_yaml("test: value", "test.yaml")
        
        assert isinstance(result, ValidationResult)
        assert hasattr(result, 'is_valid')
        assert hasattr(result, 'errors')
        assert hasattr(result, 'warnings')
        assert hasattr(result, 'line_numbers')
        assert isinstance(result.errors, list)
        assert isinstance(result.warnings, list)
        assert isinstance(result.line_numbers, list)
