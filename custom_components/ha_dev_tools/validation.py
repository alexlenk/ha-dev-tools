"""Validation manager for the Home Assistant Management Integration."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List

import yaml

_LOGGER = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of content validation."""
    is_valid: bool
    errors: List[str]
    warnings: List[str]
    line_numbers: List[int]


class ValidationManager:
    """Handles YAML/JSON validation and schema compliance."""

    def __init__(self) -> None:
        """Initialize the validation manager."""
        self.yaml_loader = yaml.SafeLoader
        _LOGGER.info("ValidationManager initialized")

    def validate_content(self, content: str, file_path: str) -> ValidationResult:
        """
        Validate content based on file extension.
        
        Args:
            content: Content to validate
            file_path: File path to determine validation type
            
        Returns:
            ValidationResult with validation status and errors
        """
        file_extension = Path(file_path).suffix.lower()
        
        if file_extension in ['.yaml', '.yml']:
            return self.validate_yaml(content, file_path)
        elif file_extension == '.json':
            return self.validate_json(content, file_path)
        else:
            # For other file types, just return valid
            return ValidationResult(
                is_valid=True,
                errors=[],
                warnings=[],
                line_numbers=[]
            )

    def validate_yaml(self, content: str, file_path: str) -> ValidationResult:
        """
        Validate YAML syntax and basic Home Assistant schema compliance.
        
        Args:
            content: YAML content to validate
            file_path: File path for context
            
        Returns:
            ValidationResult with validation status and errors
        """
        errors = []
        warnings = []
        line_numbers = []
        
        try:
            # Parse YAML content
            parsed_data = yaml.safe_load(content)
            
            # Basic validation for configuration.yaml
            if file_path == 'configuration.yaml':
                if not isinstance(parsed_data, dict):
                    errors.append("Configuration must be a dictionary")
                    line_numbers.append(1)
                else:
                    # Check for required homeassistant section
                    if 'homeassistant' not in parsed_data:
                        warnings.append("Missing 'homeassistant' section in configuration")
            
            _LOGGER.debug("YAML validation successful for %s", file_path)
            
        except yaml.YAMLError as e:
            error_msg = f"YAML syntax error: {str(e)}"
            errors.append(error_msg)
            
            # Try to extract line number from error
            if hasattr(e, 'problem_mark') and e.problem_mark:
                line_numbers.append(e.problem_mark.line + 1)
            else:
                line_numbers.append(1)
                
            _LOGGER.warning("YAML validation failed for %s: %s", file_path, error_msg)
        
        except Exception as e:
            error_msg = f"Unexpected validation error: {str(e)}"
            errors.append(error_msg)
            line_numbers.append(1)
            _LOGGER.error("Unexpected YAML validation error for %s: %s", file_path, e)
        
        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            line_numbers=line_numbers
        )

    def validate_json(self, content: str, file_path: str) -> ValidationResult:
        """
        Validate JSON syntax and schema compliance.
        
        Args:
            content: JSON content to validate
            file_path: File path for context
            
        Returns:
            ValidationResult with validation status and errors
        """
        errors = []
        warnings = []
        line_numbers = []
        
        try:
            # Parse JSON content
            parsed_data = json.loads(content)
            _LOGGER.debug("JSON validation successful for %s", file_path)
            
        except json.JSONDecodeError as e:
            error_msg = f"JSON syntax error: {str(e)}"
            errors.append(error_msg)
            line_numbers.append(getattr(e, 'lineno', 1))
            _LOGGER.warning("JSON validation failed for %s: %s", file_path, error_msg)
        
        except Exception as e:
            error_msg = f"Unexpected validation error: {str(e)}"
            errors.append(error_msg)
            line_numbers.append(1)
            _LOGGER.error("Unexpected JSON validation error for %s: %s", file_path, e)
        
        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            line_numbers=line_numbers
        )

    def validate_storage_file(self, content: str, storage_type: str) -> ValidationResult:
        """
        Validate Home Assistant storage file format.
        
        Args:
            content: Storage file content
            storage_type: Type of storage file
            
        Returns:
            ValidationResult with validation status and errors
        """
        # For now, just validate as JSON since storage files are JSON
        result = self.validate_json(content, f".storage/{storage_type}")
        
        # Add storage-specific validation if needed
        if result.is_valid:
            try:
                data = json.loads(content)
                if not isinstance(data, dict):
                    result.errors.append("Storage file must be a JSON object")
                    result.is_valid = False
                elif 'version' not in data:
                    result.warnings.append("Storage file missing version field")
            except Exception:
                pass  # Already handled by JSON validation
        
        return result