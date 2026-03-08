"""File manager for the Home Assistant Management Integration."""
from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

from homeassistant.core import HomeAssistant

from .const import ERROR_FILE_NOT_FOUND, ERROR_BLACKLISTED_FILE
from .security import SecurityManager

_LOGGER = logging.getLogger(__name__)


class FileManager:
    """Handles file system operations with security validation."""

    def __init__(self, hass: HomeAssistant, security_manager: SecurityManager) -> None:
        """Initialize the file manager."""
        self.hass = hass
        self.security_manager = security_manager
        self.config_dir = hass.config.config_dir  # Keep as string for compatibility
        self._config_path = Path(hass.config.config_dir)  # Use Path internally
        
        _LOGGER.info("FileManager initialized for config directory: %s", self.config_dir)

    async def read_file(self, file_path: str) -> str:
        """
        Read file contents with security validation.
        
        Args:
            file_path: Path to the file to read
            
        Returns:
            File content as string
            
        Raises:
            FileNotFoundError: If file doesn't exist
            PermissionError: If access is denied
            ValueError: If path validation fails
        """
        # Validate file path security
        is_valid, error = self.security_manager.validate_file_path(file_path)
        if not is_valid:
            if error == ERROR_FILE_NOT_FOUND:
                raise FileNotFoundError(f"File not found: {file_path}")
            elif error == ERROR_BLACKLISTED_FILE:
                raise PermissionError(f"Access to blacklisted file denied: {file_path}")
            else:
                raise ValueError(f"Invalid file path: {error}")
        
        try:
            full_path = self._config_path / file_path
            
            # Check if file exists
            if not full_path.exists():
                _LOGGER.warning("File not found: %s", full_path)
                raise FileNotFoundError(f"File not found: {file_path}")
            
            # Check if it's actually a file (not a directory)
            if not full_path.is_file():
                _LOGGER.warning("Path is not a file: %s", full_path)
                raise FileNotFoundError(f"Path is not a file: {file_path}")
            
            # Read file contents
            content = await self.hass.async_add_executor_job(
                self._read_file_sync, full_path
            )
            
            _LOGGER.debug("Successfully read file: %s (%d bytes)", file_path, len(content))
            return content
            
        except (FileNotFoundError, PermissionError, ValueError):
            raise
        except UnicodeDecodeError as e:
            _LOGGER.error("File encoding error reading: %s", file_path)
            raise ValueError(f"File encoding error: {str(e)}")
        except Exception as e:
            _LOGGER.error("Unexpected error reading file %s: %s", file_path, e)
            raise RuntimeError(f"Error reading file: {str(e)}")

    def _read_file_sync(self, file_path: Path) -> str:
        """
        Synchronous file reading helper.
        
        Args:
            file_path: Path object to read
            
        Returns:
            File contents as string
        """
        with open(file_path, 'r', encoding='utf-8', newline='') as f:
            return f.read()

    async def write_file(
        self, 
        file_path: str, 
        content: str, 
        expected_hash: Optional[str] = None,
        validate_before_write: bool = True
    ) -> Dict[str, Any]:
        """
        Write file with validation, security checks, and atomic operations.
        
        Args:
            file_path: Path to the file to write
            content: Content to write to the file
            expected_hash: Expected current hash for conflict detection (optional)
            validate_before_write: Whether to validate YAML syntax before writing
            
        Returns:
            Dictionary with new file metadata after write
            
        Raises:
            PermissionError: If access is denied
            ValueError: If path validation fails, content is invalid, or hash conflict detected
        """
        # Validate file path security
        is_valid, error = self.security_manager.validate_file_path(file_path)
        if not is_valid:
            if error == ERROR_BLACKLISTED_FILE:
                raise PermissionError(f"Access to blacklisted file denied: {file_path}")
            else:
                raise ValueError(f"Invalid file path: {error}")
        
        # Validate YAML/JSON content before writing (if enabled)
        if validate_before_write:
            from .validation import ValidationManager
            validator = ValidationManager()
            validation_result = validator.validate_content(content, file_path)
            if not validation_result.is_valid:
                raise ValueError(f"Content validation failed: {'; '.join(validation_result.errors)}")
        
        try:
            full_path = self._config_path / file_path
            
            # Check expected_hash if provided (conflict detection)
            if expected_hash is not None and full_path.exists():
                current_metadata = await self.get_file_metadata(file_path)
                if current_metadata.get("content_hash") != expected_hash:
                    raise ValueError(
                        f"Hash conflict: file has been modified. "
                        f"Expected {expected_hash}, got {current_metadata.get('content_hash')}"
                    )
            
            # Create backup if file exists
            if full_path.exists():
                await self._create_backup(full_path)
            
            # Ensure parent directory exists
            full_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Write file contents atomically (write to temp, then rename)
            await self.hass.async_add_executor_job(
                self._write_file_atomic, full_path, content
            )
            
            _LOGGER.info("Successfully wrote file: %s (%d bytes)", file_path, len(content))
            
            # Return new metadata after write
            new_metadata = await self.get_file_metadata(file_path)
            return new_metadata
            
        except (PermissionError, ValueError):
            raise
        except Exception as e:
            _LOGGER.error("Unexpected error writing file %s: %s", file_path, e)
            raise RuntimeError(f"Error writing file: {str(e)}")

    def _write_file_sync(self, file_path: Path, content: str) -> None:
        """
        Synchronous file writing helper.
        
        Args:
            file_path: Path object to write
            content: Content to write
        """
        with open(file_path, 'w', encoding='utf-8', newline='') as f:
            f.write(content)

    def _write_file_atomic(self, file_path: Path, content: str) -> None:
        """
        Synchronous atomic file writing helper.
        Writes to a temporary file first, then renames to target path.
        
        Args:
            file_path: Path object to write
            content: Content to write
        """
        import tempfile
        
        # Write to temporary file in the same directory
        temp_fd, temp_path = tempfile.mkstemp(
            dir=file_path.parent,
            prefix=f".{file_path.name}.",
            suffix=".tmp"
        )
        
        try:
            # Write content to temp file
            with os.fdopen(temp_fd, 'w', encoding='utf-8', newline='') as f:
                f.write(content)
            
            # Atomically rename temp file to target
            temp_path_obj = Path(temp_path)
            temp_path_obj.replace(file_path)
            
        except Exception:
            # Clean up temp file on error
            try:
                Path(temp_path).unlink(missing_ok=True)
            except Exception:
                pass
            raise

    async def _create_backup(self, file_path: Path) -> None:
        """
        Create a backup of the file before overwriting.
        
        Args:
            file_path: Path to the file to backup
        """
        try:
            # Create backup directory structure
            backup_dir = self._config_path / ".ha_dev_tools_backups"
            backup_dir.mkdir(exist_ok=True)
            
            # Create timestamped backup filename
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            rel_path = file_path.relative_to(self._config_path)
            backup_name = f"{rel_path.name}.{timestamp}.backup"
            backup_path = backup_dir / backup_name
            
            # Copy file to backup location
            await self.hass.async_add_executor_job(
                self._copy_file_sync, file_path, backup_path
            )
            
            _LOGGER.debug("Created backup: %s", backup_path)
            
        except Exception as e:
            # Log backup failure but don't fail the write operation
            _LOGGER.warning("Failed to create backup for %s: %s", file_path, e)

    def _copy_file_sync(self, source: Path, destination: Path) -> None:
        """
        Synchronous file copy helper.
        
        Args:
            source: Source file path
            destination: Destination file path
        """
        import shutil
        shutil.copy2(source, destination)

    async def file_exists(self, file_path: str) -> bool:
        """
        Check if file exists with security validation.
        
        Args:
            file_path: Path to check
            
        Returns:
            True if file exists
            
        Raises:
            ValueError: If path validation fails
        """
        # Validate file path security
        is_valid, error = self.security_manager.validate_file_path(file_path)
        if not is_valid:
            raise ValueError(f"Invalid file path: {error}")
        
        try:
            full_path = self._config_path / file_path
            exists = full_path.exists() and full_path.is_file()
            return exists
            
        except Exception as e:
            _LOGGER.error("Error checking file existence %s: %s", file_path, e)
            raise RuntimeError(f"Error checking file: {str(e)}")

    async def list_files(self, directory: str = "") -> list:
        """
        List configuration files with metadata.
        
        Args:
            directory: Optional subdirectory to list (relative to config dir)
            
        Returns:
            List of file info dictionaries
            
        Raises:
            ValueError: If path validation fails
            PermissionError: If access is denied
        """
        try:
            base_path = self._config_path
            if directory:
                # Validate directory path
                is_valid, error = self.security_manager.validate_file_path(directory)
                if not is_valid:
                    raise ValueError(f"Invalid directory path: {error}")
                base_path = self._config_path / directory
            
            if not base_path.exists():
                return []
            
            # List files
            files = []
            for item in base_path.iterdir():
                try:
                    # Get relative path from config dir
                    rel_path = item.relative_to(self._config_path)
                    
                    # Skip blacklisted files
                    if self.security_manager.is_blacklisted(str(rel_path)):
                        continue
                    
                    # Get file info
                    stat = item.stat()
                    file_info = {
                        "name": item.name,
                        "path": str(rel_path),
                        "size": stat.st_size,
                        "modified": stat.st_mtime,
                        "is_directory": item.is_dir(),
                    }
                    files.append(file_info)
                    
                except Exception as e:
                    _LOGGER.debug("Skipping file %s: %s", item, e)
                    continue
            
            _LOGGER.debug("Listed %d files in %s", len(files), base_path)
            return files
            
        except (ValueError, PermissionError):
            raise
        except Exception as e:
            _LOGGER.error("Error listing files in %s: %s", directory, e)
            raise RuntimeError(f"Error listing files: {str(e)}")

    async def delete_file(self, file_path: str) -> bool:
        """
        Delete file with security validation.
        
        Args:
            file_path: Path to the file to delete
            
        Returns:
            True if successful
            
        Raises:
            FileNotFoundError: If file doesn't exist
            PermissionError: If access is denied
            ValueError: If path validation fails
        """
        # Validate file path security
        is_valid, error = self.security_manager.validate_file_path(file_path)
        if not is_valid:
            if error == ERROR_BLACKLISTED_FILE:
                raise PermissionError(f"Access to blacklisted file denied: {file_path}")
            else:
                raise ValueError(f"Invalid file path: {error}")
        
        try:
            full_path = self._config_path / file_path
            
            # Check if file exists
            if not full_path.exists():
                _LOGGER.warning("File not found for deletion: %s", full_path)
                raise FileNotFoundError(f"File not found: {file_path}")
            
            # Check if it's actually a file (not a directory)
            if not full_path.is_file():
                _LOGGER.warning("Path is not a file: %s", full_path)
                raise ValueError("Cannot delete directory")
            
            # Delete file
            await self.hass.async_add_executor_job(full_path.unlink)
            
            _LOGGER.info("Successfully deleted file: %s", file_path)
            return True
            
        except (FileNotFoundError, PermissionError, ValueError):
            raise
        except Exception as e:
            _LOGGER.error("Unexpected error deleting file %s: %s", file_path, e)
            raise RuntimeError(f"Error deleting file: {str(e)}")

    async def get_file_metadata(self, file_path: str) -> Dict[str, Any]:
        """
        Get file metadata without reading full content.
        
        Args:
            file_path: Path to the file
            
        Returns:
            Dictionary with metadata fields:
                - path: str - Relative file path
                - size: int - File size in bytes
                - modified_at: str - ISO 8601 timestamp
                - content_hash: str - SHA-256 hash
                - exists: bool - Whether file exists
                - accessible: bool - Whether file is accessible
                
        Raises:
            ValueError: If path validation fails
        """
        # Validate file path security
        is_valid, error = self.security_manager.validate_file_path(file_path)
        
        # Build base metadata
        metadata = {
            "path": file_path,
            "exists": False,
            "accessible": is_valid,
        }
        
        # If not accessible, return early with limited info
        if not is_valid:
            _LOGGER.debug("File not accessible: %s (reason: %s)", file_path, error)
            return metadata
        
        try:
            full_path = self._config_path / file_path
            
            # Check if file exists
            if not full_path.exists() or not full_path.is_file():
                _LOGGER.debug("File does not exist: %s", file_path)
                return metadata
            
            # File exists and is accessible
            metadata["exists"] = True
            
            # Get file stats and hash
            stat_info, content_hash = await self.hass.async_add_executor_job(
                self._get_file_stats_and_hash, full_path
            )
            
            metadata.update({
                "size": stat_info["size"],
                "modified_at": stat_info["modified_at"],
                "content_hash": content_hash,
            })
            
            _LOGGER.debug("Retrieved metadata for file: %s", file_path)
            return metadata
            
        except Exception as e:
            _LOGGER.error("Error getting metadata for %s: %s", file_path, e)
            raise RuntimeError(f"Error getting file metadata: {str(e)}")

    def _get_file_stats_and_hash(self, file_path: Path) -> Tuple[Dict[str, Any], str]:
        """
        Synchronous helper to get file stats and calculate hash.
        
        Args:
            file_path: Path object to analyze
            
        Returns:
            Tuple of (stat_info dict, content_hash string)
        """
        # Get file stats
        stat = file_path.stat()
        stat_info = {
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }
        
        # Calculate SHA-256 hash
        sha256_hash = hashlib.sha256()
        with open(file_path, 'rb') as f:
            # Read in chunks to handle large files efficiently
            for chunk in iter(lambda: f.read(8192), b''):
                sha256_hash.update(chunk)
        
        content_hash = sha256_hash.hexdigest()
        
        return stat_info, content_hash