# Home Assistant Configuration Manager Integration

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)
[![hacs][hacsbadge]][hacs]

A comprehensive Home Assistant custom integration that provides secure read-only REST API endpoints for configuration file access and log retrieval. This integration enables external development tools to programmatically view Home Assistant configuration files and retrieve logs from the core system through authenticated API calls.

## Features

### Configuration File Management (Read-Only)
- **Read Files**: Access configuration.yaml and other config files via REST API
- **List Files**: Browse available configuration files
- **No Write Operations**: API is read-only for security - no file creation, modification, or deletion

### Log Access (Read-Only)
- **Core Logs**: Retrieve Home Assistant core system logs
- **Filtered Logs**: Filter logs by lines, time range, and severity level
- **Structured Output**: JSON-formatted log responses

### Security Features
- **Admin-Only Access**: All endpoints require administrator authentication
- **Blacklist Protection**: Sensitive files (secrets.yaml, .HA_VERSION) are protected
- **Path Validation**: Prevents directory traversal attacks
- **File Extension Whitelist**: Only allowed file types can be accessed
- **Security Logging**: All access attempts are logged for monitoring

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/your-username/ha-config-manager`
6. Select category: "Integration"
7. Click "Add"
8. Find "Home Assistant Configuration Manager" in the integration list
9. Click "Download"
10. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [releases page][releases]
2. Extract the `ha_config_manager` folder from the archive
3. Copy the folder to your `custom_components` directory:
   ```
   <config_directory>/custom_components/ha_config_manager/
   ```
4. Restart Home Assistant

## Configuration

### Required Configuration

**IMPORTANT**: Starting with version 2.0.0, this integration requires security configuration in your `configuration.yaml`. The integration will not load without it.

Add the following to your `configuration.yaml`:

### Recommended Production Configuration (Read-Only, Strict)

```yaml
ha_config_manager:
  security:
    # Read-only paths (safe for viewing, not modifying)
    read_paths:
      # Storage files for dashboards and helpers
      - "/config/.storage/lovelace*"
      - "/config/.storage/input_*"
      - "/config/.storage/timer"
      - "/config/.storage/counter"
      - "/config/.storage/script"
      - "/config/.storage/scene"
      - "/config/.storage/automation"
      
      # Main configuration files
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
      - "/config/scripts.yaml"
      - "/config/scenes.yaml"
      
      # Package files
      - "/config/packages/**/*.yaml"
    
    # Write-enabled paths (for future use - currently all read-only)
    write_paths: []
      # Future: Enable writes for specific use cases
      # - "/config/packages/generated/*.yaml"
    
    # Always denied (sensitive files)
    denied_paths:
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
      - "/config/secrets.yaml"
      - "/config/.HA_VERSION"
```

This configuration:
- ✅ Allows **read-only** access to dashboards, helpers, and main config files
- ✅ Prevents **all write operations** by default (security-first approach)
- ✅ Blocks access to sensitive authentication and system files
- ✅ Uses glob patterns for flexible path matching
- ✅ Provides commented examples for future write enablement

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `read_paths` | list | `[]` | Paths that allow read-only operations |
| `write_paths` | list | `[]` | Paths that allow both read and write operations |
| `denied_paths` | list | `[]` | Paths that are always blocked (merged with defaults) |

### Security Model

The integration operates in **strict allowlist mode** where only explicitly permitted paths are accessible. This ensures security by default - all access is denied unless explicitly allowed.

**Key principles:**
- Deny by default - only explicitly permitted paths are accessible
- Denylist always enforced - sensitive files are always blocked
- Read-only by default - write operations require explicit configuration

### Path Patterns

The integration supports glob patterns for flexible path matching:

- `*` - Matches any characters (e.g., `/config/.storage/lovelace*` matches all Lovelace dashboards)
- `?` - Matches a single character (e.g., `config.yam?` matches `config.yaml` and `config.yml`)
- `**` - Matches directories recursively (e.g., `/config/packages/**/*.yaml` matches all YAML files in packages)

**Pattern Examples:**

```yaml
# Match all Lovelace dashboards
- "/config/.storage/lovelace*"
# Matches: lovelace, lovelace.dashboard_main, lovelace.dashboard_mobile

# Match all input helpers
- "/config/.storage/input_*"
# Matches: input_boolean, input_number, input_text, input_select, input_datetime

# Match all YAML files in packages recursively
- "/config/packages/**/*.yaml"
# Matches: packages/lights.yaml, packages/sensors/temp.yaml, packages/deep/nested/config.yaml

# Match both .yaml and .yml extensions
- "/config/config.yam?"
# Matches: config.yaml, config.yml
```

For more pattern examples and use cases, see [Configuration Examples](docs/CONFIGURATION_EXAMPLES.md).

### Read-Only vs Read-Write Access

**Read-Only Paths** (`read_paths`):
- Files can be viewed but not modified
- Recommended for configuration files like `configuration.yaml`
- Prevents accidental modifications

**Read-Write Paths** (`write_paths`):
- Files can be both viewed and modified
- Currently not recommended (future feature)
- Requires explicit configuration

**Denied Paths** (`denied_paths`):
- Files are completely inaccessible
- Always enforced regardless of other settings
- Protects sensitive authentication and system files

### Migration from Previous Versions

If you're upgrading from a version before 2.0.0, the integration will fail to load with a helpful error message containing the recommended configuration. Simply copy the recommended production configuration above into your `configuration.yaml` and restart Home Assistant.

For detailed migration instructions and additional configuration examples, see [Configuration Examples](docs/CONFIGURATION_EXAMPLES.md).

### Additional Resources

- **[Configuration Examples](docs/CONFIGURATION_EXAMPLES.md)** - Copy-paste ready configurations for common use cases
- **[API Documentation](#api-endpoints)** - Complete API reference
- **[Security Guide](#security)** - Security best practices and protected files

## API Endpoints

All endpoints require authentication with an admin-level access token.

**IMPORTANT: This API is READ-ONLY. No write, update, or delete operations are supported for security reasons.**

### File Operations (Read-Only)

#### List Files
```http
GET /api/management/files
```

Returns a list of all accessible configuration files.

Query parameters:
- `directory`: Optional subdirectory to list (default: root config directory)

Example:
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://homeassistant.local:8123/api/management/files
```

#### Read File
```http
GET /api/management/files/{filepath}
```

Returns the contents of the specified file as plain text.

Example:
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://homeassistant.local:8123/api/management/files/configuration.yaml
```

Response:
```
Content-Type: text/plain
Status: 200 OK

homeassistant:
  name: Home
  latitude: 32.87336
  ...
```

### File Metadata Operations

#### Get File Metadata
```http
GET /api/ha_config_manager/metadata/{filepath}
```

Returns metadata for a single file without reading its full content. Useful for version checking and conflict detection.

**Response Format:**
```json
{
  "path": "configuration.yaml",
  "size": 2048,
  "modified_at": "2026-03-06T10:30:45.123456",
  "content_hash": "a3b5c7d9e1f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4",
  "exists": true,
  "accessible": true
}
```

**Fields:**
- `path`: Relative path from `/config` directory
- `size`: File size in bytes
- `modified_at`: ISO 8601 timestamp of last modification
- `content_hash`: SHA-256 hash of file content (hex encoded)
- `exists`: Whether the file exists
- `accessible`: Whether the file is within allowed paths

**Example:**
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://homeassistant.local:8123/api/ha_config_manager/metadata/configuration.yaml
```

**Response:**
```json
{
  "path": "configuration.yaml",
  "size": 2048,
  "modified_at": "2026-03-06T10:30:45.123456",
  "content_hash": "a3b5c7d9e1f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4",
  "exists": true,
  "accessible": true
}
```

**Use Cases:**
- Check if a file has been modified since last download
- Detect version conflicts before uploading changes
- Track file changes over time
- Verify file integrity

**Error Responses:**

File not found (404):
```json
{
  "error": "File not found",
  "path": "nonexistent.yaml"
}
```

Access denied (403):
```json
{
  "error": "Access denied",
  "path": "secrets.yaml",
  "reason": "File is in denied paths"
}
```

#### Batch Get Metadata
```http
POST /api/ha_config_manager/metadata/batch
```

Returns metadata for multiple files in a single request. More efficient than multiple individual requests.

**Request Body:**
```json
{
  "file_paths": [
    "configuration.yaml",
    "automations.yaml",
    "scripts.yaml"
  ]
}
```

**Response Format:**
```json
{
  "metadata": [
    {
      "path": "configuration.yaml",
      "size": 2048,
      "modified_at": "2026-03-06T10:30:45.123456",
      "content_hash": "a3b5c7d9e1f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4",
      "exists": true,
      "accessible": true
    },
    {
      "path": "automations.yaml",
      "size": 4096,
      "modified_at": "2026-03-06T11:15:30.654321",
      "content_hash": "b4c6d8e0f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6",
      "exists": true,
      "accessible": true
    },
    {
      "path": "scripts.yaml",
      "size": 1024,
      "modified_at": "2026-03-05T09:45:12.987654",
      "content_hash": "c5d7e9f1a3b5c7d9e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9b1c3d5e7f9a1b3c5d7",
      "exists": true,
      "accessible": true
    }
  ],
  "errors": []
}
```

**Batch Size Limit:** Maximum 20 files per request

**Example:**
```bash
curl -X POST \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "file_paths": [
      "configuration.yaml",
      "automations.yaml",
      "scripts.yaml"
    ]
  }' \
  http://homeassistant.local:8123/api/ha_config_manager/metadata/batch
```

**Partial Failures:**

If some files are inaccessible or don't exist, they are reported in the `errors` array:

```json
{
  "metadata": [
    {
      "path": "configuration.yaml",
      "size": 2048,
      "modified_at": "2026-03-06T10:30:45.123456",
      "content_hash": "a3b5c7d9e1f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4",
      "exists": true,
      "accessible": true
    }
  ],
  "errors": [
    {
      "path": "secrets.yaml",
      "error": "Access denied",
      "reason": "File is in denied paths"
    },
    {
      "path": "nonexistent.yaml",
      "error": "File not found"
    }
  ]
}
```

**Use Cases:**
- Check multiple files for modifications in one request
- Efficient version checking for related files
- Batch conflict detection before uploading changes
- Monitor file changes across multiple configuration files

### File Write Operations

#### Write File
```http
PUT /api/ha_config_manager/files/{filepath}
```

Writes content to a configuration file. Only available for paths in `write_paths` configuration.

**Request Body:**
```json
{
  "content": "homeassistant:\n  name: Home\n  latitude: 32.87336\n",
  "expected_hash": "a3b5c7d9e1f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4",
  "validate_before_write": true
}
```

**Fields:**
- `content` (required): File content to write
- `expected_hash` (optional): Expected current hash for conflict detection
- `validate_before_write` (optional, default: true): Validate YAML syntax before writing

**Response Format:**
```json
{
  "success": true,
  "path": "packages/generated/lights.yaml",
  "metadata": {
    "path": "packages/generated/lights.yaml",
    "size": 1024,
    "modified_at": "2026-03-06T12:00:00.000000",
    "content_hash": "d6e8f0a2b4c6d8e0f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8",
    "exists": true,
    "accessible": true
  },
  "config_check": {
    "valid": true,
    "errors": []
  }
}
```

**Example:**
```bash
curl -X PUT \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "content": "light:\n  - platform: template\n    lights:\n      living_room:\n        friendly_name: Living Room Light\n",
    "validate_before_write": true
  }' \
  http://homeassistant.local:8123/api/ha_config_manager/files/packages/generated/lights.yaml
```

**Conflict Detection:**

If `expected_hash` is provided and doesn't match the current file hash, a conflict is detected:

```json
{
  "error": "Version conflict",
  "message": "File has been modified since last read",
  "current_hash": "e7f9a1b3c5d7e9f1a3b5c7d9e1f3a5b7c9d1e3f5a7b9c1d3e5f7a9b1c3d5e7f9",
  "expected_hash": "a3b5c7d9e1f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4",
  "path": "packages/generated/lights.yaml"
}
```

**Validation Errors:**

If YAML validation fails:

```json
{
  "error": "Validation failed",
  "message": "Invalid YAML syntax",
  "details": "mapping values are not allowed here\n  in \"<unicode string>\", line 3, column 10",
  "path": "packages/generated/lights.yaml"
}
```

**Access Denied:**

If the file is not in `write_paths`:

```json
{
  "error": "Access denied",
  "message": "Write access not allowed for this path",
  "path": "configuration.yaml",
  "reason": "Path not in write_paths configuration"
}
```

**Rate Limit Exceeded:**

If rate limit is exceeded:

```json
{
  "error": "Rate limit exceeded",
  "message": "Too many write operations",
  "limit": "10 writes per minute",
  "retry_after": 45
}
```

### Log Operations (Read-Only)

#### Get Core Logs
```http
GET /api/management/logs/core
```

Returns the most recent core logs in JSON format.

Query parameters:
- `lines`: Number of log lines to return (default: 100)
- `level`: Filter by log level (DEBUG, INFO, WARNING, ERROR)
- `search`: Search for specific text in logs
- `offset`: Offset for pagination (default: 0)
- `limit`: Maximum number of entries to return (default: 100)

Example:
```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "http://homeassistant.local:8123/api/management/logs/core?lines=50&level=ERROR"
```

Response:
```json
{
  "logs": [
    {
      "timestamp": "2026-02-10T10:30:45",
      "level": "ERROR",
      "message": "Error loading component",
      "source": "homeassistant.loader"
    }
  ],
  "total_count": 50,
  "source": "core"
}
```

## Security

### Authentication

All API endpoints require a valid Home Assistant long-lived access token with administrator privileges.

To create a token:
1. Go to your Home Assistant profile
2. Scroll to "Long-Lived Access Tokens"
3. Click "Create Token"
4. Give it a name (e.g., "Config Manager API")
5. Copy the token and use it in your API requests

### Write Operations Security

**IMPORTANT**: Write operations are disabled by default for security. Enable them only for specific paths where you need programmatic file modification.

#### Enabling Write Operations

To enable write operations for specific paths, add them to the `write_paths` configuration:

```yaml
ha_config_manager:
  security:
    write_paths:
      # Enable writes for generated automation files
      - "/config/packages/generated/*.yaml"
      
      # Enable writes for test configurations
      - "/config/test_configs/*.yaml"
```

#### Write Operation Security Features

1. **Admin-Only Access**: All write operations require administrator authentication
2. **Explicit Allowlist**: Only paths in `write_paths` can be modified
3. **YAML Validation**: All YAML files are validated before writing
4. **Atomic Writes**: Files are written atomically (temp file + rename) to prevent corruption
5. **Automatic Backups**: Original files are backed up before modification
6. **Configuration Checks**: Config files trigger automatic HA configuration validation
7. **Conflict Detection**: Optional hash-based conflict detection prevents overwriting newer versions

#### Rate Limiting

Write operations are rate-limited to prevent abuse:

**Default Limits:**
- **10 writes per minute** per user
- **100 writes per hour** per user
- **1000 writes per day** per user

**Configuring Rate Limits:**

```yaml
ha_config_manager:
  security:
    write_paths:
      - "/config/packages/generated/*.yaml"
    
    rate_limiting:
      enabled: true
      writes_per_minute: 10
      writes_per_hour: 100
      writes_per_day: 1000
```

To disable rate limiting (not recommended):

```yaml
ha_config_manager:
  security:
    rate_limiting:
      enabled: false
```

#### Audit Logging

All write operations are logged for security auditing:

**Logged Information:**
- Timestamp of operation
- User who performed the operation
- File path modified
- Operation type (create, update, delete)
- Success or failure status
- Validation results
- Conflict detection results

**Log Location:** Home Assistant system logs with `ha_config_manager.security` source

**Example Log Entries:**

```
2026-03-06 10:30:45 INFO (MainThread) [ha_config_manager.security] 
  Write operation: user=admin, path=/config/packages/generated/lights.yaml, 
  operation=update, status=success, validated=true

2026-03-06 10:31:12 WARNING (MainThread) [ha_config_manager.security] 
  Write operation denied: user=admin, path=/config/secrets.yaml, 
  reason=denied_path

2026-03-06 10:32:05 ERROR (MainThread) [ha_config_manager.security] 
  Write operation failed: user=admin, path=/config/automations.yaml, 
  reason=validation_failed, error=invalid YAML syntax at line 15
```

#### Security Best Practices

**DO:**
- ✅ Use the most restrictive `write_paths` possible
- ✅ Enable write access only for generated or temporary files
- ✅ Keep rate limiting enabled in production
- ✅ Monitor audit logs regularly
- ✅ Use conflict detection (`expected_hash`) for critical files
- ✅ Test write operations in a development environment first
- ✅ Back up your configuration before enabling write operations

**DON'T:**
- ❌ Enable write access to `configuration.yaml` unless absolutely necessary
- ❌ Use wildcard patterns that match sensitive files
- ❌ Disable rate limiting in production environments
- ❌ Ignore validation errors
- ❌ Skip backup verification
- ❌ Grant write access to `.storage/` files
- ❌ Use write operations for files that should be managed through HA UI

#### Example: Safe Write Configuration

```yaml
ha_config_manager:
  security:
    # Read-only access to main config files
    read_paths:
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
      - "/config/scripts.yaml"
      - "/config/.storage/lovelace*"
    
    # Write access only to generated files
    write_paths:
      - "/config/packages/generated/*.yaml"
      - "/config/test_configs/*.yaml"
    
    # Always deny sensitive files
    denied_paths:
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
    
    # Enable rate limiting
    rate_limiting:
      enabled: true
      writes_per_minute: 10
      writes_per_hour: 100
      writes_per_day: 1000
```

This configuration:
- Allows reading main configuration files
- Restricts writes to specific generated/test directories
- Protects sensitive files from all access
- Enforces rate limiting to prevent abuse

### Protected Files

The following files are **always denied** and cannot be accessed regardless of configuration:
- `secrets.yaml` - Contains sensitive credentials
- `.HA_VERSION` - System version file
- `home-assistant.log` - Core log file (use log API instead)
- `.storage/auth*` - Authentication data
- `.storage/core.*` - Core system registries
- `.cloud` - Cloud integration data
- `.uuid` - System identifier
- Database files

### Path Security

- **Path Validation**: All file paths must be within the `/config` directory
- **Traversal Protection**: Path traversal attempts (`../`) are automatically blocked
- **Glob Support**: Patterns like `*` and `?` are supported for flexible matching
- **Normalization**: Paths are normalized before validation to prevent bypasses
- **Operation Tracking**: Read and write operations are tracked separately

### Security Logging

All security events are logged for audit purposes:
- Denied access attempts
- Path traversal attempts
- Invalid configuration warnings
- Security rule changes
- Write operations (success and failure)
- Rate limit violations
- Validation failures

## Use Cases

### External Development Tools (Read-Only Access)

Use this integration with external development tools to:
- View configuration files from your IDE
- Monitor configuration state
- Analyze logs in real-time
- Validate configurations before manual deployment

**Note**: This integration provides read-only access. File modifications must be done through Home Assistant's UI or direct file system access.

### MCP Server Integration

This integration works seamlessly with the Configuration Manager MCP Server to provide:
- IDE integration for Home Assistant development
- Real-time configuration viewing
- Log monitoring and analysis
- Configuration analysis and validation

### Monitoring and Analysis

Integrate with monitoring tools to:
- Track configuration changes over time
- Monitor system logs for errors
- Analyze configuration patterns
- Generate configuration reports

## Development

### Testing

The integration includes comprehensive tests:

```bash
# Set up environment
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-test.txt

# Run tests
cd src/ha-integration
PYTHONPATH=. python -m pytest tests/ -v

# Run with coverage
PYTHONPATH=. python -m pytest tests/ --cov=custom_components/ha_config_manager
```

### Property-Based Testing

The integration uses property-based testing with Hypothesis to validate correctness properties:

```bash
# Run property tests
PYTHONPATH=. python -m pytest tests/property/ -v --hypothesis-show-statistics
```

## Troubleshooting

### Integration Not Loading

1. Check Home Assistant logs for errors
2. Verify the integration is in the correct directory
3. Ensure all dependencies are installed
4. Restart Home Assistant

### API Endpoints Not Working

1. Verify you're using an admin-level access token
2. Check that the HTTP component is loaded
3. Review security logs for blocked requests
4. Ensure file paths are within allowed directories

### File Access Denied

1. Check if the file is in the blacklist
2. Verify the file extension is allowed
3. Ensure the path doesn't contain traversal attempts
4. Review security manager logs

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Ensure all tests pass
6. Submit a pull request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

- [Issue Tracker][issues]
- [Documentation][documentation]
- [Home Assistant Community Forum](https://community.home-assistant.io/)

## Acknowledgments

- Built with [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
- Uses [Hypothesis](https://hypothesis.readthedocs.io/) for property-based testing
- Follows [Home Assistant integration development guidelines](https://developers.home-assistant.io/)

---

[releases-shield]: https://img.shields.io/github/release/your-username/ha-config-manager.svg?style=for-the-badge
[releases]: https://github.com/your-username/ha-config-manager/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/your-username/ha-config-manager.svg?style=for-the-badge
[commits]: https://github.com/your-username/ha-config-manager/commits/main
[license-shield]: https://img.shields.io/github/license/your-username/ha-config-manager.svg?style=for-the-badge
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[issues]: https://github.com/your-username/ha-config-manager/issues
[documentation]: https://github.com/your-username/ha-config-manager
