"""Constants for the HA Dev Tools integration."""

DOMAIN = "ha_dev_tools"

# Options flow key for the dry-run toggle (see access_control.is_dry_run()).
OPT_DRY_RUN = "dry_run"

# Operation types
OPERATION_READ = "read"
OPERATION_WRITE = "write"

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

# Default write paths - exactly what write_automation can actually target
# (AutomationManager.candidate_files(): automations.yaml, packages/**/*.yaml).
# No YAML/UI config currently populates SecurityManager with anything else, so
# leaving this empty (as it was before) meant every write was silently
# rejected out of the box - not a safety margin, just a broken default.
DEFAULT_WRITE_PATHS = [
    "/config/automations.yaml",
    "/config/packages/**/*.yaml",
]

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
    # The dev_tools access-control arm file (see access_control.py) - must
    # never be writable through our own generic file tools, only through
    # out-of-band filesystem access (SSH, Terminal add-on). Denylisted here
    # as defense in depth even though access_control.py's own read/touch
    # never goes through this path in the first place.
    ".storage/ha_dev_tools.armed",
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

# Error codes
ERROR_INVALID_PATH = "INVALID_PATH"
ERROR_BLACKLISTED_FILE = "BLACKLISTED_FILE"
ERROR_FILE_NOT_FOUND = "FILE_NOT_FOUND"
ERROR_PERMISSION_DENIED = "PERMISSION_DENIED"
ERROR_WRITE_NOT_PERMITTED = "WRITE_NOT_PERMITTED"
