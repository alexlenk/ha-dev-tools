# HA Dev Tools v1.0.0 - Initial Release

## Overview

First public release of the HA Dev Tools custom integration for Home Assistant. This integration provides a comprehensive REST API for development tools, enabling external applications (like the ha-dev-tools-mcp server) to interact with your Home Assistant configuration files, templates, entities, and system information.

## Features

### File Management
- Read configuration files (configuration.yaml, automations.yaml, etc.)
- List files in the config directory
- Validate YAML syntax
- Get file metadata (size, modification time, permissions)

### Template Testing
- Render Jinja2 templates with Home Assistant context
- Validate template syntax
- Test templates with entity states
- Support for all Home Assistant template functions

### Entity & State Management
- Query entity states
- List all entities
- Filter entities by domain
- Access entity attributes

### System Information
- Get Home Assistant version
- Check system health
- Access configuration details
- View integration status

### Security
- Configurable file access allowlist
- Path traversal protection
- Read-only file operations
- Secure API authentication

## Installation

### Via HACS (Recommended)

1. Open HACS in Home Assistant
2. Click on "Integrations"
3. Click the three dots in the top right corner
4. Select "Custom repositories"
5. Add this repository URL: `https://github.com/alexlenk/ha-dev-tools`
6. Select category: "Integration"
7. Click "Add"
8. Search for "HA Dev Tools" in HACS
9. Click "Download"
10. Restart Home Assistant
11. Add the integration via Settings → Devices & Services → Add Integration

### Manual Installation

1. Download the latest release
2. Extract the `custom_components/ha_dev_tools` directory
3. Copy it to your Home Assistant `custom_components` directory
4. Restart Home Assistant
5. Add the integration via Settings → Devices & Services → Add Integration

## Configuration

The integration supports optional configuration for security:

```yaml
# configuration.yaml
ha_dev_tools:
  allowed_paths:
    - "*.yaml"
    - "*.yml"
    - "*.json"
    - "blueprints/**"
    - "custom_components/**"
```

## API Endpoints

All endpoints are available at `/api/ha_dev_tools/*`:

- `GET /api/ha_dev_tools/files` - List configuration files
- `GET /api/ha_dev_tools/files/read` - Read a file
- `POST /api/ha_dev_tools/templates/render` - Render a template
- `POST /api/ha_dev_tools/templates/validate` - Validate template syntax
- `GET /api/ha_dev_tools/entities` - List entities
- `GET /api/ha_dev_tools/entities/{entity_id}` - Get entity state
- `GET /api/ha_dev_tools/system/info` - Get system information

## Requirements

- Home Assistant 2024.1.0 or later
- Python 3.12 or later

## Related Projects

- **MCP Server**: [ha-dev-tools-mcp](https://github.com/alexlenk/ha-dev-tools-mcp) - MCP server for Kiro IDE integration
- **Kiro Power**: [ha-development-power](https://github.com/alexlenk/ha-development-power) - Kiro Power package

## Documentation

- [README](README.md) - Full documentation
- [API Documentation](docs/API.md) - API reference
- [Security Guide](docs/SECURITY.md) - Security best practices
- [Contributing](CONTRIBUTING.md) - Contribution guidelines

## Support

- [GitHub Issues](https://github.com/alexlenk/ha-dev-tools/issues) - Bug reports and feature requests
- [GitHub Discussions](https://github.com/alexlenk/ha-dev-tools/discussions) - Questions and community support

## License

MIT License - See [LICENSE](LICENSE) for details

## Changelog

### v1.0.0 (2026-03-08)

**Initial Release**

- File management API with read-only access
- Template rendering and validation
- Entity state queries
- System information endpoints
- Configurable security allowlist
- HACS compatibility
- Comprehensive test suite
- Full documentation
