"""Data models for the Home Assistant Management Integration."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SecurityConfiguration:
    """Represents the security configuration.
    
    The system operates in strict allowlist mode where only explicitly permitted
    paths are accessible. All access is denied by default.
    
    This dataclass encapsulates all security-related configuration including
    allowed paths, denied paths, and read/write permissions.
    """
    
    read_paths: List[str] = field(default_factory=list)
    write_paths: List[str] = field(default_factory=list)
    denied_paths: List[str] = field(default_factory=list)
    allowed_paths: List[str] = field(default_factory=list)  # Legacy support
    
    # Legacy field for backward compatibility
    allowed_storage_files: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert the SecurityConfiguration to a dictionary.
        
        Returns:
            Dict[str, Any]: Dictionary representation of the configuration
        """
        return {
            "read_paths": self.read_paths,
            "write_paths": self.write_paths,
            "denied_paths": self.denied_paths,
            "allowed_paths": self.allowed_paths,
            "allowed_storage_files": self.allowed_storage_files,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SecurityConfiguration":
        """Create a SecurityConfiguration from a dictionary.
        
        Args:
            data: Dictionary containing configuration data
            
        Returns:
            SecurityConfiguration: New instance created from the dictionary
        """
        return cls(
            read_paths=data.get("read_paths", []),
            write_paths=data.get("write_paths", []),
            denied_paths=data.get("denied_paths", []),
            allowed_paths=data.get("allowed_paths", []),
            allowed_storage_files=data.get("allowed_storage_files", []),
        )


@dataclass
class PathValidationResult:
    """Result of path validation.
    
    This dataclass encapsulates the result of validating a file path against
    security rules, including whether access is permitted, error messages,
    and which rule was matched.
    """
    
    is_valid: bool
    error_message: Optional[str] = None
    matched_rule: Optional[str] = None
    rule_type: Optional[str] = None
    operation: str = "read"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert the PathValidationResult to a dictionary.
        
        Returns:
            Dict[str, Any]: Dictionary representation of the validation result
        """
        return {
            "is_valid": self.is_valid,
            "error_message": self.error_message,
            "matched_rule": self.matched_rule,
            "rule_type": self.rule_type,
            "operation": self.operation,
        }
