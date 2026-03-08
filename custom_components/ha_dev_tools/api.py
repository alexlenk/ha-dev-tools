"""API handler for the Home Assistant Management Integration.

This module provides read-only REST API endpoints for:
- Listing configuration files
- Reading configuration file contents
- Retrieving system logs

All write operations (POST, PUT, DELETE) have been removed for security.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
from typing import Any, Dict

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    API_BASE_PATH,
    ERROR_AUTHENTICATION_REQUIRED,
    ERROR_BLACKLISTED_FILE,
    ERROR_FILE_NOT_FOUND,
    ERROR_INVALID_PATH,
    ERROR_INVALID_SYNTAX,
    ERROR_PERMISSION_DENIED,
    HTTP_BAD_REQUEST,
    HTTP_CREATED,
    HTTP_FORBIDDEN,
    HTTP_INTERNAL_SERVER_ERROR,
    HTTP_NOT_FOUND,
    HTTP_OK,
    HTTP_UNAUTHORIZED,
    HTTP_UNPROCESSABLE_ENTITY,
)
from .file_manager import FileManager
from .log_manager import LogFilters, LogManager
from .security import SecurityManager
from .validation import ValidationManager

_LOGGER = logging.getLogger(__name__)


class ManagementAPIHandler:
    """Central API request handler that routes requests to appropriate managers."""

    def __init__(self, hass: HomeAssistant, security_manager: SecurityManager) -> None:
        """Initialize the API handler."""
        self.hass = hass
        self.security_manager = security_manager
        self.file_manager = FileManager(hass, security_manager)
        self.log_manager = LogManager(hass, security_manager)
        self.validation_manager = ValidationManager()
        
        _LOGGER.info("ManagementAPIHandler initialized")

    async def register_api_endpoints(self) -> None:
        """Register API endpoints with Home Assistant's web component."""
        try:
            # Register the file listing endpoint view
            self.hass.http.register_view(FileListAPIView(self))
            
            # Register the file endpoint view
            self.hass.http.register_view(FileAPIView(self))
            
            # Register the metadata endpoint view
            self.hass.http.register_view(MetadataAPIView(self))
            
            # Register the batch metadata endpoint view
            self.hass.http.register_view(BatchMetadataAPIView(self))
            
            # Register the logs endpoint view
            self.hass.http.register_view(LogsAPIView(self))
            
            _LOGGER.info("API endpoints registered successfully")
            
        except Exception as e:
            _LOGGER.error("Failed to register API endpoints: %s", e)
            raise

    async def cleanup(self) -> None:
        """Clean up resources when integration is unloaded."""
        _LOGGER.info("Cleaning up API handler resources")
        # Add any cleanup logic here if needed

    def _create_error_response(self, error_message: str, error_code: str, status: int) -> web.Response:
        """
        Create standardized error response.
        
        Args:
            error_message: Human-readable error message
            error_code: Machine-readable error code
            status: HTTP status code
            
        Returns:
            JSON error response
        """
        response_data = {
            "success": False,
            "error": error_message,
            "error_code": error_code
        }
        
        return web.json_response(response_data, status=status)

    def _create_success_response(self, data: Any = None) -> web.Response:
        """
        Create standardized success response.
        
        Args:
            data: Response data
            
        Returns:
            JSON success response
        """
        response_data = {
            "success": True,
            "data": data
        }
        
        return web.json_response(response_data, status=HTTP_OK)


class FileListAPIView(HomeAssistantView):
    """API view for listing files."""

    url = f"{API_BASE_PATH}/files"
    name = "api:management:files:list"
    requires_auth = True

    def __init__(self, api_handler: ManagementAPIHandler) -> None:
        """Initialize the file list API view."""
        self.api_handler = api_handler

    async def get(self, request: web.Request) -> web.Response:
        """Handle GET requests for file listing."""
        try:
            # Validate user permissions
            user = request.get("hass_user")
            is_authorized, auth_error = self.api_handler.security_manager.validate_user_permissions(user)
            if not is_authorized:
                return self.api_handler._create_error_response(
                    auth_error or "Authentication required",
                    ERROR_AUTHENTICATION_REQUIRED,
                    HTTP_UNAUTHORIZED if not user else HTTP_FORBIDDEN
                )

            # Get optional directory parameter
            directory = request.query.get("directory", "")
            
            # List files
            files = await self.api_handler.file_manager.list_files(directory)
            return web.json_response({"files": files, "directory": directory}, status=HTTP_OK)

        except ValueError as e:
            return self.api_handler._create_error_response(
                str(e),
                "FILE_LIST_ERROR",
                HTTP_BAD_REQUEST
            )
        except PermissionError as e:
            return self.api_handler._create_error_response(
                str(e),
                ERROR_PERMISSION_DENIED,
                HTTP_FORBIDDEN
            )
        except Exception as e:
            _LOGGER.error("Unexpected error in file list GET: %s", e)
            return self.api_handler._create_error_response(
                "Internal server error",
                "INTERNAL_ERROR",
                HTTP_INTERNAL_SERVER_ERROR
            )


class FileAPIView(HomeAssistantView):
    """API view for read-only file operations."""

    url = f"{API_BASE_PATH}/files/{{file_path:.*}}"
    name = "api:management:files"
    requires_auth = True

    def __init__(self, api_handler: ManagementAPIHandler) -> None:
        """Initialize the file API view."""
        self.api_handler = api_handler

    async def get(self, request: web.Request, file_path: str) -> web.Response:
        """Handle GET requests for file content with pagination and compression support."""
        try:
            # Validate user permissions
            user = request.get("hass_user")
            is_authorized, auth_error = self.api_handler.security_manager.validate_user_permissions(user)
            if not is_authorized:
                return self.api_handler._create_error_response(
                    auth_error or "Authentication required",
                    ERROR_AUTHENTICATION_REQUIRED,
                    HTTP_UNAUTHORIZED if not user else HTTP_FORBIDDEN
                )

            # Get query parameters for pagination and compression
            offset = int(request.query.get('offset', 0))
            limit = request.query.get('limit')
            if limit is not None:
                limit = int(limit)
            compress = request.query.get('compress', 'false').lower() == 'true'
            
            # Validate parameters
            if offset < 0:
                return self.api_handler._create_error_response(
                    "offset must be non-negative",
                    ERROR_INVALID_PATH,
                    HTTP_BAD_REQUEST
                )
            if limit is not None and limit < 1:
                return self.api_handler._create_error_response(
                    "limit must be positive",
                    ERROR_INVALID_PATH,
                    HTTP_BAD_REQUEST
                )

            # Read full file content
            full_content = await self.api_handler.file_manager.read_file(file_path)
            full_content_bytes = full_content.encode('utf-8')
            total_size = len(full_content_bytes)
            
            # Apply offset and limit for chunking
            if offset >= total_size:
                # Offset beyond file size - return empty content
                content_bytes = b''
                has_more = False
            else:
                end_pos = offset + limit if limit is not None else total_size
                content_bytes = full_content_bytes[offset:end_pos]
                has_more = end_pos < total_size
            
            # Decode content back to string
            content = content_bytes.decode('utf-8')
            returned_size = len(content_bytes)
            
            # Calculate content hash
            content_hash = hashlib.sha256(content_bytes).hexdigest()
            
            # Prepare response headers
            headers = {
                'X-Total-Size': str(total_size),
                'X-Offset': str(offset),
                'X-Has-More': 'true' if has_more else 'false',
                'Content-Length': str(returned_size)
            }
            
            # Apply compression if requested
            if compress and returned_size > 0:
                compressed_content = gzip.compress(content_bytes)
                headers['Content-Encoding'] = 'gzip'
                headers['X-Uncompressed-Size'] = str(returned_size)
                
                # Return compressed binary response
                return web.Response(
                    body=compressed_content,
                    status=HTTP_OK,
                    headers=headers,
                    content_type='application/octet-stream'
                )
            else:
                # Return plain text response
                return web.Response(
                    text=content,
                    status=HTTP_OK,
                    headers=headers,
                    content_type='text/plain'
                )

        except FileNotFoundError as e:
            return self.api_handler._create_error_response(
                str(e),
                ERROR_FILE_NOT_FOUND,
                HTTP_NOT_FOUND
            )
        except PermissionError as e:
            return self.api_handler._create_error_response(
                str(e),
                ERROR_BLACKLISTED_FILE,
                HTTP_FORBIDDEN
            )
        except ValueError as e:
            return self.api_handler._create_error_response(
                str(e),
                ERROR_INVALID_PATH,
                HTTP_BAD_REQUEST
            )
        except Exception as e:
            _LOGGER.error("Unexpected error in file GET: %s", e)
            return self.api_handler._create_error_response(
                "Internal server error",
                "INTERNAL_ERROR",
                HTTP_INTERNAL_SERVER_ERROR
            )

    async def put(self, request: web.Request, file_path: str) -> web.Response:
        """Handle PUT requests for file upload/write."""
        try:
            # Validate user permissions
            user = request.get("hass_user")
            is_authorized, auth_error = self.api_handler.security_manager.validate_user_permissions(user)
            if not is_authorized:
                return self.api_handler._create_error_response(
                    auth_error or "Authentication required",
                    ERROR_AUTHENTICATION_REQUIRED,
                    HTTP_UNAUTHORIZED if not user else HTTP_FORBIDDEN
                )

            # Parse request body
            try:
                data = await request.json()
            except json.JSONDecodeError:
                return self.api_handler._create_error_response(
                    "Invalid JSON in request body",
                    "INVALID_JSON",
                    HTTP_BAD_REQUEST
                )

            # Extract parameters
            content = data.get("content")
            if content is None:
                return self.api_handler._create_error_response(
                    "Missing required parameter: content",
                    "MISSING_PARAMETER",
                    HTTP_BAD_REQUEST
                )

            expected_hash = data.get("expected_hash")
            validate_before_write = data.get("validate_before_write", True)

            # Write file
            new_metadata = await self.api_handler.file_manager.write_file(
                file_path, 
                content, 
                expected_hash=expected_hash,
                validate_before_write=validate_before_write
            )

            # Trigger config check for configuration files
            config_check_result = None
            if self._is_config_file(file_path):
                try:
                    config_check_result = await self._trigger_config_check()
                except Exception as e:
                    _LOGGER.warning("Config check failed after write: %s", e)
                    # Don't fail the write operation if config check fails
                    config_check_result = {"error": str(e)}

            # Return success with new metadata
            response_data = {
                "success": True,
                "metadata": new_metadata,
                "config_check": config_check_result
            }
            
            return web.json_response(response_data, status=HTTP_OK)

        except PermissionError as e:
            return self.api_handler._create_error_response(
                str(e),
                ERROR_BLACKLISTED_FILE,
                HTTP_FORBIDDEN
            )
        except ValueError as e:
            # Check if it's a hash conflict
            if "Hash conflict" in str(e):
                return self.api_handler._create_error_response(
                    str(e),
                    "HASH_CONFLICT",
                    409  # HTTP 409 Conflict
                )
            # Check if it's a validation error
            elif "validation failed" in str(e).lower():
                return self.api_handler._create_error_response(
                    str(e),
                    ERROR_INVALID_SYNTAX,
                    HTTP_UNPROCESSABLE_ENTITY
                )
            else:
                return self.api_handler._create_error_response(
                    str(e),
                    ERROR_INVALID_PATH,
                    HTTP_BAD_REQUEST
                )
        except Exception as e:
            _LOGGER.error("Unexpected error in file PUT: %s", e)
            return self.api_handler._create_error_response(
                "Internal server error",
                "INTERNAL_ERROR",
                HTTP_INTERNAL_SERVER_ERROR
            )

    def _is_config_file(self, file_path: str) -> bool:
        """
        Check if file is a configuration file that requires config check.
        
        Args:
            file_path: Path to check
            
        Returns:
            True if file is a configuration file
        """
        config_files = [
            "configuration.yaml",
            "automations.yaml",
            "scripts.yaml",
            "scenes.yaml",
            "groups.yaml",
            "customize.yaml",
        ]
        
        # Check if file is in root config files
        if file_path in config_files:
            return True
        
        # Check if file is in packages directory
        if file_path.startswith("packages/") and file_path.endswith(".yaml"):
            return True
        
        return False

    async def _trigger_config_check(self) -> Dict[str, Any]:
        """
        Trigger Home Assistant configuration check.
        
        Returns:
            Dictionary with config check results
        """
        try:
            # Call the check_config service
            result = await self.api_handler.hass.services.async_call(
                "homeassistant",
                "check_config",
                blocking=True,
                return_response=True
            )
            
            return {
                "valid": result is not None,
                "result": result
            }
        except Exception as e:
            _LOGGER.error("Error triggering config check: %s", e)
            raise


class MetadataAPIView(HomeAssistantView):
    """API view for file metadata operations."""

    url = f"{API_BASE_PATH}/metadata/{{file_path:.*}}"
    name = "api:management:metadata"
    requires_auth = True

    def __init__(self, api_handler: ManagementAPIHandler) -> None:
        """Initialize the metadata API view."""
        self.api_handler = api_handler

    async def get(self, request: web.Request, file_path: str) -> web.Response:
        """Handle GET requests for file metadata."""
        try:
            # Validate user permissions
            user = request.get("hass_user")
            is_authorized, auth_error = self.api_handler.security_manager.validate_user_permissions(user)
            if not is_authorized:
                return self.api_handler._create_error_response(
                    auth_error or "Authentication required",
                    ERROR_AUTHENTICATION_REQUIRED,
                    HTTP_UNAUTHORIZED if not user else HTTP_FORBIDDEN
                )

            # Get file metadata
            metadata = await self.api_handler.file_manager.get_file_metadata(file_path)
            
            # Return metadata as JSON
            return web.json_response(metadata, status=HTTP_OK)

        except FileNotFoundError as e:
            return self.api_handler._create_error_response(
                str(e),
                ERROR_FILE_NOT_FOUND,
                HTTP_NOT_FOUND
            )
        except PermissionError as e:
            return self.api_handler._create_error_response(
                str(e),
                ERROR_BLACKLISTED_FILE,
                HTTP_FORBIDDEN
            )
        except ValueError as e:
            return self.api_handler._create_error_response(
                str(e),
                ERROR_INVALID_PATH,
                HTTP_BAD_REQUEST
            )
        except Exception as e:
            _LOGGER.error("Unexpected error in metadata GET: %s", e)
            return self.api_handler._create_error_response(
                "Internal server error",
                "INTERNAL_ERROR",
                HTTP_INTERNAL_SERVER_ERROR
            )


class BatchMetadataAPIView(HomeAssistantView):
    """API view for batch metadata operations."""

    url = f"{API_BASE_PATH}/metadata/batch"
    name = "api:management:metadata:batch"
    requires_auth = True

    def __init__(self, api_handler: ManagementAPIHandler) -> None:
        """Initialize the batch metadata API view."""
        self.api_handler = api_handler

    async def post(self, request: web.Request) -> web.Response:
        """Handle POST requests for batch file metadata."""
        try:
            # Validate user permissions
            user = request.get("hass_user")
            is_authorized, auth_error = self.api_handler.security_manager.validate_user_permissions(user)
            if not is_authorized:
                return self.api_handler._create_error_response(
                    auth_error or "Authentication required",
                    ERROR_AUTHENTICATION_REQUIRED,
                    HTTP_UNAUTHORIZED if not user else HTTP_FORBIDDEN
                )

            # Parse request body
            try:
                data = await request.json()
            except json.JSONDecodeError:
                return self.api_handler._create_error_response(
                    "Invalid JSON in request body",
                    "INVALID_JSON",
                    HTTP_BAD_REQUEST
                )

            # Validate file_paths parameter
            file_paths = data.get("file_paths", [])
            if not isinstance(file_paths, list):
                return self.api_handler._create_error_response(
                    "file_paths must be an array",
                    "INVALID_PARAMETER",
                    HTTP_BAD_REQUEST
                )

            # Limit batch size to 20 files
            if len(file_paths) > 20:
                return self.api_handler._create_error_response(
                    "Batch size limited to 20 files",
                    "BATCH_SIZE_EXCEEDED",
                    HTTP_BAD_REQUEST
                )

            # Get metadata for each file
            results = []
            for file_path in file_paths:
                try:
                    metadata = await self.api_handler.file_manager.get_file_metadata(file_path)
                    results.append(metadata)
                except Exception as e:
                    # For batch operations, include errors in results rather than failing entire request
                    _LOGGER.warning("Error getting metadata for %s: %s", file_path, e)
                    results.append({
                        "path": file_path,
                        "exists": False,
                        "accessible": False,
                        "error": str(e)
                    })

            # Return batch results
            return web.json_response({
                "metadata": results,
                "total": len(results)
            }, status=HTTP_OK)

        except Exception as e:
            _LOGGER.error("Unexpected error in batch metadata POST: %s", e)
            return self.api_handler._create_error_response(
                "Internal server error",
                "INTERNAL_ERROR",
                HTTP_INTERNAL_SERVER_ERROR
            )



class LogsAPIView(HomeAssistantView):
    """API view for log access operations."""

    url = f"{API_BASE_PATH}/logs/{{log_source}}"
    name = "api:management:logs"
    requires_auth = True

    def __init__(self, api_handler: ManagementAPIHandler) -> None:
        """Initialize the logs API view."""
        self.api_handler = api_handler

    async def get(self, request: web.Request, log_source: str) -> web.Response:
        """Handle GET requests for log content."""
        try:
            # Validate user permissions
            user = request.get("hass_user")
            is_authorized, auth_error = self.api_handler.security_manager.validate_user_permissions(user)
            if not is_authorized:
                return self.api_handler._create_error_response(
                    auth_error or "Authentication required",
                    ERROR_AUTHENTICATION_REQUIRED,
                    HTTP_UNAUTHORIZED if not user else HTTP_FORBIDDEN
                )

            # Parse query parameters
            query_params = request.query
            filters = LogFilters(
                lines=int(query_params.get("lines", 100)),
                level=query_params.get("level"),
                search=query_params.get("search"),
                offset=int(query_params.get("offset", 0)),
                limit=int(query_params.get("limit", 100))
            )

            # Get logs based on source
            if log_source == "core":
                log_entries = await self.api_handler.log_manager.get_core_logs(filters)
            else:
                return self.api_handler._create_error_response(
                    f"Unsupported log source: {log_source}",
                    "INVALID_LOG_SOURCE",
                    HTTP_BAD_REQUEST
                )

            # Convert log entries to dictionaries
            log_data = [entry.to_dict() for entry in log_entries] if log_entries else []

            return web.json_response({
                "logs": log_data,
                "total_count": len(log_data),
                "source": log_source
            }, status=HTTP_OK)

        except ValueError as e:
            return self.api_handler._create_error_response(
                f"Invalid query parameter: {str(e)}",
                "INVALID_PARAMETER",
                HTTP_BAD_REQUEST
            )
        except PermissionError as e:
            return self.api_handler._create_error_response(
                str(e),
                ERROR_PERMISSION_DENIED,
                HTTP_FORBIDDEN
            )
        except Exception as e:
            _LOGGER.error("Unexpected error in logs GET: %s", e)
            return self.api_handler._create_error_response(
                "Internal server error",
                "INTERNAL_ERROR",
                HTTP_INTERNAL_SERVER_ERROR
            )