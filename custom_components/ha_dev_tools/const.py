"""Constants for the Home Assistant Management Integration."""

import voluptuous as vol
from homeassistant.helpers import config_validation as cv

# Domain name for the integration
DOMAIN = "ha_dev_tools"

# API endpoints base path
API_BASE_PATH = "/api/management"

# Operation types
OPERATION_READ = "read"
OPERATION_WRITE = "write"

# Security modes
SECURITY_MODE_ALLOWLIST = "allowlist"
SECURITY_MODE_DENYLIST = "denylist"

# Recommended safe storage patterns (glob patterns for safe .storage files)
RECOMMENDED_SAFE_STORAGE_PATTERNS = [
    "/config/.storage/lovelace*",
    "/config/.storage/input_*",
    "/config/.storage/timer",
    "/config/.storage/counter",
    "/config/.storage/script",
    "/config/.storage/scene",
    "/config/.storage/automation",
]

# Default read-only paths (recommended configuration)
DEFAULT_READ_ONLY_PATHS = [
    "/config/.storage/lovelace*",
    "/config/.storage/input_*",
    "/config/.storage/timer",
    "/config/.storage/counter",
    "/config/.storage/script",
    "/config/.storage/scene",
    "/config/.storage/automation",
    "/config/configuration.yaml",
    "/config/automations.yaml",
    "/config/scripts.yaml",
    "/config/scenes.yaml",
    "/config/packages/**/*.yaml",
]

# Default write paths (empty by default, for future use)
DEFAULT_WRITE_PATHS = []

# Default denylist (always enforced) - includes sensitive storage files with patterns
DEFAULT_DENYLIST = [
    "secrets.yaml",
    ".HA_VERSION",
    "home-assistant.log",
    ".storage/auth*",
    ".storage/auth_provider.homeassistant",
    ".storage/core.config_entries",
    ".storage/core.device_registry",
    ".storage/core.entity_registry",
    ".storage/core.*",
    ".storage/onboarding",
    ".storage/hassio",
    ".cloud",
    ".uuid",
    "known_devices.yaml",
    ".storage/person",
    ".storage/zone",
]

# Backward compatibility alias
DEFAULT_BLACKLIST = DEFAULT_DENYLIST

# Allowed file extensions for security
ALLOWED_EXTENSIONS = {
    ".yaml",
    ".yml", 
    ".json",
    ".txt",
    ".py",
    ".jinja2",
}

# Allowed directories for file access
ALLOWED_DIRECTORIES = {
    "/config",
    "/addon_configs",
}

# Default allowlist paths (used when allowlist mode enabled with no config)
DEFAULT_ALLOWLIST_PATHS = [
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml",
    "scenes.yaml",
    "packages",
    "blueprints",
    "custom_components",
]

# Legacy: Directory/file allowlist (whitelist) - only these paths are accessible
# Empty list means all non-blacklisted files are allowed (current behavior)
# When populated, ONLY these paths and their subdirectories are accessible
DEFAULT_ALLOWLIST = []

# HTTP status codes
HTTP_OK = 200
HTTP_CREATED = 201
HTTP_BAD_REQUEST = 400
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE_ENTITY = 422
HTTP_INTERNAL_SERVER_ERROR = 500

# Error codes
ERROR_INVALID_PATH = "INVALID_PATH"
ERROR_BLACKLISTED_FILE = "BLACKLISTED_FILE"
ERROR_DENYLISTED_FILE = "DENYLISTED_FILE"
ERROR_INVALID_SYNTAX = "INVALID_SYNTAX"
ERROR_SCHEMA_VIOLATION = "SCHEMA_VIOLATION"
ERROR_FILE_NOT_FOUND = "FILE_NOT_FOUND"
ERROR_PERMISSION_DENIED = "PERMISSION_DENIED"
ERROR_AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"
ERROR_WRITE_NOT_PERMITTED = "WRITE_NOT_PERMITTED"

# Configuration schema for security settings
SECURITY_CONFIG_SCHEMA = vol.Schema({
    vol.Optional("read_paths", default=[]): vol.All(
        cv.ensure_list,
        [cv.string]
    ),
    vol.Optional("write_paths", default=[]): vol.All(
        cv.ensure_list,
        [cv.string]
    ),
    vol.Optional("denied_paths", default=[]): vol.All(
        cv.ensure_list,
        [cv.string]
    ),
    # Legacy support for old configuration format
    vol.Optional("allowed_paths", default=[]): vol.All(
        cv.ensure_list,
        [cv.string]
    ),
    vol.Optional("allowed_storage_files", default=[]): vol.All(
        cv.ensure_list,
        [cv.string]
    ),
})

# Configuration schema for the ha_dev_tools domain
CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional("security", default={}): SECURITY_CONFIG_SCHEMA,
    })
}, extra=vol.ALLOW_EXTRA)