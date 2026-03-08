# HA Dev Tools - Home Assistant Development Integration

[![GitHub Release][releases-shield]][releases]
[![GitHub Activity][commits-shield]][commits]
[![License][license-shield]](LICENSE)
[![hacs][hacsbadge]][hacs]

A comprehensive Home Assistant custom integration that provides secure REST API endpoints for configuration file access, log retrieval, and development tools. This integration enables external development tools to programmatically interact with Home Assistant configuration files and system logs through authenticated API calls.

**Part of the HA Dev Tools ecosystem:**
- **[ha-dev-tools-mcp](https://github.com/alexlenk/ha-dev-tools-mcp)** - MCP server for IDE integration
- **[ha-development-power](https://github.com/alexlenk/ha-development-power)** - Kiro Power for seamless development workflow
- **ha-dev-tools** (this repository) - Home Assistant integration providing the API

## Features

### Configuration File Management
- **Read Files**: Access configuration.yaml and other config files via REST API
- **List Files**: Browse available configuration files
- **Write Files**: Programmatically update configuration files (with security controls)
- **Metadata Access**: Get file metadata for version tracking and conflict detection

### Log Access (Read-Only)
- **Core Logs**: Retrieve Home Assistant core system logs
- **Filtered Logs**: Filter logs by lines, time range, and severity level
- **Structured Output**: JSON-formatted log responses

### Security Features
- **Admin-Only Access**: All endpoints require administrator authentication
- **Allowlist Protection**: Only explicitly permitted paths are accessible
- **Denylist Protection**: Sensitive files (secrets.yaml, auth data) are always blocked
- **Path Validation**: Prevents directory traversal attacks
- **Security Logging**: All access attempts are logged for monitoring
- **Rate Limiting**: Write operations are rate-limited to prevent abuse
- **YAML Validation**: Automatic validation before writing configuration files
- **Conflict Detection**: Hash-based conflict detection prevents overwriting newer versions

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/alexlenk/ha-dev-tools`
6. Select category: "Integration"
7. Click "Add"
8. Find "HA Dev Tools" in the integration list
9. Click "Download"
10. Restart Home Assistant

### Manual Installation

1. Download the latest release from the [releases page][releases]
2. Extract the `ha_dev_tools` folder from the archive
3. Copy the folder to your `custom_components` directory:
   ```
   <config_directory>/custom_components/ha_dev_tools/
   ```
4. Restart Home Assistant

## Configuration

### Required Configuration

**IMPORTANT**: This integration requires security configuration in your `configuration.yaml`. The integration will not load without it.

Add the following to your `configuration.yaml`:

### Recommended Production Configuration (Read-Only, Strict)

```yaml
ha_dev_tools:
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
- Requires explicit configuration
- Subject to rate limiting and validation

**Denied Paths** (`denied_paths`):
- Files are completely inaccessible
- Always enforced regardless of other settings
- Protects sensitive authentication and system files

### Additional Resources

- **[Configuration Examples](docs/CONFIGURATION_EXAMPLES.md)** - Copy-paste ready configurations for common use cases
- **[API Documentation](#api-endpoints)** - Complete API reference
- **[Security Guide](#security)** - Security best practices and protected files

## API Endpoints

All endpoints require authentication with an admin-level access token.

### File Operations

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
GET /api/ha_dev_tools/metadata/{filepath}
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
  http://homeassistant.local:8123/api/ha_dev_tools/metadata/configuration.yaml
```

**Use Cases:**
- Check if a file has been modified since last download
- Detect version conflicts before uploading changes
- Track file changes over time
- Verify file integrity

#### Batch Get Metadata
```http
POST /api/ha_dev_tools/metadata/batch
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
    }
  ],
  "errors": []
}
```

**Batch Size Limit:** Maximum 20 files per request

### File Write Operations

#### Write File
```http
PUT /api/ha_dev_tools/files/{filepath}
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
  http://homeassistant.local:8123/api/ha_dev_tools/files/packages/generated/lights.yaml
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

For complete API documentation including error responses and security details, see [API Documentation](docs/API.md).

## Security

### Authentication

All API endpoints require a valid Home Assistant long-lived access token with administrator privileges.

To create a token:
1. Go to your Home Assistant profile
2. Scroll to "Long-Lived Access Tokens"
3. Click "Create Token"
4. Give it a name (e.g., "HA Dev Tools API")
5. Copy the token and use it in your API requests

### Write Operations Security

**IMPORTANT**: Write operations are disabled by default for security. Enable them only for specific paths where you need programmatic file modification.

#### Enabling Write Operations

To enable write operations for specific paths, add them to the `write_paths` configuration:

```yaml
ha_dev_tools:
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
ha_dev_tools:
  security:
    write_paths:
      - "/config/packages/generated/*.yaml"
    
    rate_limiting:
      enabled: true
      writes_per_minute: 10
      writes_per_hour: 100
      writes_per_day: 1000
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

**Log Location:** Home Assistant system logs with `ha_dev_tools.security` source

For complete security documentation, see [Security Guide](docs/SECURITY.md).

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

## Use Cases

### IDE Integration with MCP Server

Use this integration with the [ha-dev-tools-mcp](https://github.com/alexlenk/ha-dev-tools-mcp) server to:
- Edit configuration files directly from your IDE
- View and analyze logs in real-time
- Test templates with live entity data
- Validate configurations before deployment

### Kiro Power Integration

Install the [ha-development-power](https://github.com/alexlenk/ha-development-power) Kiro Power for:
- Seamless IDE integration with Home Assistant
- Automatic MCP server configuration
- Guided workflows for common development tasks
- Template testing and validation tools

### External Development Tools

Use this integration with external development tools to:
- View configuration files from your IDE
- Monitor configuration state
- Analyze logs in real-time
- Validate configurations before manual deployment

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
PYTHONPATH=. python -m pytest tests/ -v

# Run with coverage
PYTHONPATH=. python -m pytest tests/ --cov=custom_components/ha_dev_tools
```

### Property-Based Testing

The integration uses property-based testing with Hypothesis to validate correctness properties:

```bash
# Run property tests
PYTHONPATH=. python -m pytest tests/property/ -v --hypothesis-show-statistics
```

For detailed development setup instructions, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Troubleshooting

### Integration Not Loading

1. Check Home Assistant logs for errors
2. Verify the integration is in the correct directory: `custom_components/ha_dev_tools/`
3. Ensure security configuration is present in `configuration.yaml`
4. Restart Home Assistant

### API Endpoints Not Working

1. Verify you're using an admin-level access token
2. Check that the HTTP component is loaded
3. Review security logs for blocked requests
4. Ensure file paths are within allowed directories

### File Access Denied

1. Check if the file is in the denylist
2. Verify the file path is in `read_paths` or `write_paths`
3. Ensure the path doesn't contain traversal attempts
4. Review security manager logs

For more troubleshooting help, see [Troubleshooting Guide](docs/TROUBLESHOOTING.md).

## Related Projects

This integration is part of the HA Dev Tools ecosystem:

- **[ha-dev-tools-mcp](https://github.com/alexlenk/ha-dev-tools-mcp)** - MCP server for IDE integration (Python package on PyPI)
- **[ha-development-power](https://github.com/alexlenk/ha-development-power)** - Kiro Power for seamless development workflow
- **ha-dev-tools** (this repository) - Home Assistant integration providing the API

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

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

[releases-shield]: https://img.shields.io/github/release/alexlenk/ha-dev-tools.svg?style=for-the-badge
[releases]: https://github.com/alexlenk/ha-dev-tools/releases
[commits-shield]: https://img.shields.io/github/commit-activity/y/alexlenk/ha-dev-tools.svg?style=for-the-badge
[commits]: https://github.com/alexlenk/ha-dev-tools/commits/main
[license-shield]: https://img.shields.io/github/license/alexlenk/ha-dev-tools.svg?style=for-the-badge
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[issues]: https://github.com/alexlenk/ha-dev-tools/issues
[documentation]: https://github.com/alexlenk/ha-dev-tools
