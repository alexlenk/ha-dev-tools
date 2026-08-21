# Configuration Examples

This document covers `configuration.yaml`'s `ha_dev_tools: security:` block
- the path allowlist/denylist that bounds what file-touching tools
(`write_automation` and the underlying `FileManager`) can reach. It's
independent of, and in addition to, the arm-file/admin gate covered in
[SECURITY.md](SECURITY.md) - that gate controls *when* tools run at all;
this controls *which paths* they can touch once they do. Sane defaults
apply if you never add this block at all; add it to narrow or widen access
from those defaults.

## Table of Contents

- [Recommended Production Setup](#recommended-production-setup)
- [Development Setup](#development-setup)
- [Glob Pattern Syntax](#glob-pattern-syntax)
- [Read-Only vs Read-Write Access](#read-only-vs-read-write-access)
- [Common Use Cases](#common-use-cases)
- [Migration Guide](#migration-guide)

## Recommended Production Setup

**Use this configuration for production environments where security is paramount.**

This setup provides read-only access to safe configuration files and storage files while blocking all sensitive data.

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
    
    # Write-enabled paths (empty for production - read-only mode)
    write_paths: []
      # Future: Enable writes for specific use cases
      # - "/config/packages/generated/*.yaml"
      # - "/config/.storage/input_number"  # If you want to modify helpers
    
    # Always denied (sensitive files)
    denied_paths:
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
      - "/config/secrets.yaml"
      - "/config/.HA_VERSION"
```

**What this configuration does:**
- ✅ Allows reading dashboards, helpers, scripts, automations, and scenes
- ✅ Allows reading main configuration files (configuration.yaml, etc.)
- ✅ Allows reading package files with recursive glob pattern
- ✅ Prevents ALL write operations (security-first approach)
- ✅ Blocks access to authentication and system files
- ✅ Uses glob patterns for flexible matching

## Development Setup

**Use this configuration for development environments where you need more flexibility.**

This setup adds write access to specific directories for development workflows.

```yaml
ha_dev_tools:
  security:
    # Read-only paths
    read_paths:
      # Storage files (read-only for safety)
      - "/config/.storage/lovelace*"
      - "/config/.storage/input_*"
      - "/config/.storage/timer"
      - "/config/.storage/counter"
      - "/config/.storage/script"
      - "/config/.storage/scene"
      - "/config/.storage/automation"
      
      # Main configuration files (read-only)
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
      - "/config/scripts.yaml"
      - "/config/scenes.yaml"
    
    # Write-enabled paths (for development)
    write_paths:
      # Allow writes to generated packages
      - "/config/packages/generated/*.yaml"
      
      # Allow writes to test configurations
      - "/config/test_configs/*.yaml"
      
      # Allow writes to custom blueprints
      - "/config/blueprints/custom/*.yaml"
    
    # Always denied (sensitive files)
    denied_paths:
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
      - "/config/secrets.yaml"
      - "/config/.HA_VERSION"
```

**What this configuration does:**
- ✅ Allows reading all safe configuration files
- ✅ Allows writing to specific development directories
- ✅ Keeps main configuration files read-only for safety
- ✅ Blocks access to sensitive authentication and system files

## Glob Pattern Syntax

The integration supports glob patterns for flexible path matching:

### Wildcard Patterns

#### Asterisk (`*`) - Matches any characters

```yaml
# Match all Lovelace dashboards
- "/config/.storage/lovelace*"
# Matches:
#   - /config/.storage/lovelace
#   - /config/.storage/lovelace.dashboard_main
#   - /config/.storage/lovelace.dashboard_mobile

# Match all input helpers
- "/config/.storage/input_*"
# Matches:
#   - /config/.storage/input_boolean
#   - /config/.storage/input_number
#   - /config/.storage/input_text
#   - /config/.storage/input_select
#   - /config/.storage/input_datetime

# Match all YAML files in a directory
- "/config/packages/*.yaml"
# Matches:
#   - /config/packages/lights.yaml
#   - /config/packages/sensors.yaml
#   - /config/packages/automations.yaml
```

#### Question Mark (`?`) - Matches a single character

```yaml
# Match both .yaml and .yml extensions
- "/config/config.yam?"
# Matches:
#   - /config/config.yaml
#   - /config/config.yml

# Match numbered files
- "/config/backup_?.yaml"
# Matches:
#   - /config/backup_1.yaml
#   - /config/backup_2.yaml
#   - /config/backup_a.yaml
```

#### Double Asterisk (`**`) - Matches directories recursively

```yaml
# Match all YAML files in packages and subdirectories
- "/config/packages/**/*.yaml"
# Matches:
#   - /config/packages/lights.yaml
#   - /config/packages/sensors/temperature.yaml
#   - /config/packages/automations/morning/routine.yaml
#   - /config/packages/deep/nested/path/config.yaml

# Match all files in blueprints recursively
- "/config/blueprints/**/*"
# Matches all files in all subdirectories of blueprints
```

### Pattern Examples

```yaml
# Example: Match specific storage file patterns
read_paths:
  # All Lovelace dashboards
  - "/config/.storage/lovelace*"
  
  # All input helpers (boolean, number, text, select, datetime)
  - "/config/.storage/input_*"
  
  # Specific helpers only
  - "/config/.storage/input_boolean"
  - "/config/.storage/input_number"
  
  # All core registry files (for denied_paths)
  - "/config/.storage/core.*"
  
  # All authentication files (for denied_paths)
  - "/config/.storage/auth*"
```

## Read-Only vs Read-Write Access

### Understanding Access Levels

The integration distinguishes between three types of access:

1. **Read-Only** (`read_paths`) - Files can be viewed but not modified
2. **Read-Write** (`write_paths`) - Files can be both viewed and modified
3. **Denied** (`denied_paths`) - Files are completely inaccessible

### Access Precedence Rules

The security system enforces rules in this order:

1. **Denied paths** - Always enforced first (highest priority)
2. **Write paths** - Checked for write operations
3. **Read paths** - Checked for read operations
4. **Default deny** - Everything else is blocked

```yaml
# Example showing precedence
ha_dev_tools:
  security:
    read_paths:
      - "/config/configuration.yaml"  # Read-only
    
    write_paths:
      - "/config/packages/*.yaml"     # Read and write
    
    denied_paths:
      - "/config/secrets.yaml"        # Always blocked
      - "/config/.storage/auth*"      # Always blocked
```

**How it works:**
- `/config/secrets.yaml` - ❌ Denied (in denied_paths)
- `/config/configuration.yaml` - ✅ Read allowed, ❌ Write denied (in read_paths only)
- `/config/packages/lights.yaml` - ✅ Read allowed, ✅ Write allowed (in write_paths)
- `/config/random.yaml` - ❌ Denied (not in any list, default deny)

### Write Path Precedence

If a path appears in both `read_paths` and `write_paths`, write access is granted:

```yaml
read_paths:
  - "/config/packages/*.yaml"

write_paths:
  - "/config/packages/generated/*.yaml"  # More specific, allows writes
```

Result:
- `/config/packages/lights.yaml` - ✅ Read only
- `/config/packages/generated/auto.yaml` - ✅ Read and write

### Denied Path Supremacy

Denied paths always take precedence over read or write paths:

```yaml
read_paths:
  - "/config/.storage/*"

write_paths:
  - "/config/.storage/input_*"

denied_paths:
  - "/config/.storage/auth*"  # Always blocked, even if in read/write paths
```

Result:
- `/config/.storage/input_boolean` - ✅ Read and write (in write_paths)
- `/config/.storage/lovelace` - ✅ Read only (in read_paths)
- `/config/.storage/auth` - ❌ Always denied (in denied_paths)

## Write Operations Configuration

### Enabling Write Operations

Write operations are disabled by default for security. To enable them, add paths to the `write_paths` configuration.

**Important Security Notes:**
- Write operations require admin authentication (and dev_tools' own arm-file
  gate - see [SECURITY.md](SECURITY.md))
- Only paths in `write_paths` can be modified
- All YAML files are validated before writing
- Existing files are backed up automatically before being overwritten, to a
  fixed `.ha_dev_tools_backups/` directory alongside your config - this
  isn't currently configurable (no `backup:` YAML block exists)

### Basic Write Configuration

```yaml
ha_dev_tools:
  security:
    read_paths:
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
    
    write_paths:
      # Enable writes for generated files only
      - "/config/packages/generated/*.yaml"
    
    denied_paths:
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
```

### Backups

Every write to an existing file is preceded by an automatic, timestamped
backup to `.ha_dev_tools_backups/` next to your config directory - this
isn't a separate config block, it always happens for real (not
newly-created) files, and isn't currently tunable (no retention/location
options exist yet).

## Common Use Cases

### Use Case 1: IDE Integration (Read-Only)

**Goal:** Allow external IDE to view configuration files but prevent modifications.

```yaml
ha_dev_tools:
  security:
    read_paths:
      # Main configuration files
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
      - "/config/scripts.yaml"
      - "/config/scenes.yaml"
      
      # All package files
      - "/config/packages/**/*.yaml"
      
      # Custom components
      - "/config/custom_components/**/*.py"
      
      # Blueprints
      - "/config/blueprints/**/*.yaml"
    
    write_paths: []  # No write access
    
    denied_paths:
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
```

### Use Case 2: Dashboard Management

**Goal:** Allow reading and modifying Lovelace dashboards only.

```yaml
ha_dev_tools:
  security:
    read_paths:
      # Read-only access to main config
      - "/config/configuration.yaml"
    
    write_paths:
      # Write access to dashboards
      - "/config/.storage/lovelace*"
    
    denied_paths:
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
```

### Use Case 3: Automation Development

**Goal:** Allow reading all configs and writing to automation files.

```yaml
ha_dev_tools:
  security:
    read_paths:
      # Read all configuration files
      - "/config/configuration.yaml"
      - "/config/scripts.yaml"
      - "/config/scenes.yaml"
      - "/config/packages/**/*.yaml"
      
      # Read storage files
      - "/config/.storage/lovelace*"
      - "/config/.storage/input_*"
      - "/config/.storage/script"
      - "/config/.storage/scene"
    
    write_paths:
      # Write access to automations
      - "/config/automations.yaml"
      - "/config/.storage/automation"
      
      # Write access to generated packages
      - "/config/packages/generated/*.yaml"
    
    denied_paths:
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
```

### Use Case 4: Helper Management

**Goal:** Allow reading and modifying input helpers only.

```yaml
ha_dev_tools:
  security:
    read_paths:
      # Read-only access to main config
      - "/config/configuration.yaml"
      
      # Read other storage files
      - "/config/.storage/lovelace*"
      - "/config/.storage/script"
      - "/config/.storage/automation"
    
    write_paths:
      # Write access to input helpers
      - "/config/.storage/input_boolean"
      - "/config/.storage/input_number"
      - "/config/.storage/input_text"
      - "/config/.storage/input_select"
      - "/config/.storage/input_datetime"
      - "/config/.storage/timer"
      - "/config/.storage/counter"
    
    denied_paths:
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
```

### Use Case 5: Monitoring Only (Minimal Access)

**Goal:** Allow reading logs and basic configuration for monitoring tools.

```yaml
ha_dev_tools:
  security:
    read_paths:
      # Minimal read access
      - "/config/configuration.yaml"
    
    write_paths: []  # No write access
    
    denied_paths:
      # Block everything sensitive
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
      - "/config/.storage/*"  # Block all storage files
      - "/config/automations.yaml"
      - "/config/scripts.yaml"
```

### Use Case 6: Automated Configuration Generation

**Goal:** Allow external tools to generate and write configuration files.

```yaml
ha_dev_tools:
  security:
    read_paths:
      # Read access to existing configs
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
      - "/config/scripts.yaml"
      - "/config/packages/**/*.yaml"
    
    write_paths:
      # Write access for generated files
      - "/config/packages/generated/*.yaml"
      - "/config/packages/generated/**/*.yaml"  # Recursive
    
    denied_paths:
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
    
    rate_limiting:
      enabled: true
      writes_per_minute: 20  # Higher limit for automation
      writes_per_hour: 200
      writes_per_day: 2000
```

**Use this for:**
- Automated configuration generators
- CI/CD pipelines
- Configuration management tools
- Template-based config generation

### Use Case 7: Development Workflow with Write Access

**Goal:** Full development workflow with read and write access to test files.

```yaml
ha_dev_tools:
  security:
    read_paths:
      # Read all configuration files
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
      - "/config/scripts.yaml"
      - "/config/scenes.yaml"
      - "/config/packages/**/*.yaml"
      - "/config/.storage/lovelace*"
      - "/config/.storage/input_*"
    
    write_paths:
      # Write access to test and development files
      - "/config/test_configs/*.yaml"
      - "/config/test_configs/**/*.yaml"
      - "/config/packages/dev/*.yaml"
      - "/config/packages/dev/**/*.yaml"
      
      # Write access to custom blueprints
      - "/config/blueprints/custom/*.yaml"
    
    denied_paths:
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
    
    rate_limiting:
      enabled: true
      writes_per_minute: 10
      writes_per_hour: 100
      writes_per_day: 1000
    
    backup:
      enabled: true
      retention_days: 7  # Keep backups for a week
```

**Use this for:**
- Local development with IDE integration
- Testing configuration changes
- Iterative development workflows
- Blueprint development

### Use Case 8: Package Management with Write Access

**Goal:** Allow creating and modifying package files programmatically.

```yaml
ha_dev_tools:
  security:
    read_paths:
      # Read main configuration
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
      - "/config/scripts.yaml"
      
      # Read all packages
      - "/config/packages/**/*.yaml"
    
    write_paths:
      # Write access to specific package directories
      - "/config/packages/lights/*.yaml"
      - "/config/packages/sensors/*.yaml"
      - "/config/packages/automations/*.yaml"
      
      # Write access to generated packages
      - "/config/packages/generated/**/*.yaml"
    
    denied_paths:
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
      - "/config/packages/core/*.yaml"  # Protect core packages
    
    rate_limiting:
      enabled: true
      writes_per_minute: 15
      writes_per_hour: 150
      writes_per_day: 1500
```

**Use this for:**
- Package-based configuration management
- Modular configuration development
- Automated package generation
- Configuration organization

### Use Case 9: Restricted Write Access (Production-Safe)

**Goal:** Minimal write access for production environments.

```yaml
ha_dev_tools:
  security:
    read_paths:
      # Read-only access to all configs
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
      - "/config/scripts.yaml"
      - "/config/scenes.yaml"
      - "/config/packages/**/*.yaml"
      - "/config/.storage/lovelace*"
      - "/config/.storage/input_*"
      - "/config/.storage/script"
      - "/config/.storage/scene"
      - "/config/.storage/automation"
    
    write_paths:
      # Very restricted write access
      - "/config/packages/generated/status.yaml"  # Single file only
    
    denied_paths:
      - "/config/secrets.yaml"
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
      - "/config/configuration.yaml"  # Extra protection
      - "/config/automations.yaml"
      - "/config/scripts.yaml"
    
    rate_limiting:
      enabled: true
      writes_per_minute: 5  # Very restrictive
      writes_per_hour: 50
      writes_per_day: 500
    
    backup:
      enabled: true
      retention_days: 90  # Long retention for production
      max_backups_per_file: 50
```

**Use this for:**
- Production environments with minimal write needs
- Status file updates only
- High-security deployments
- Audit-heavy environments

## Migration Guide

### Legacy Configuration Format

If you have old configuration using `allowed_paths` or `allowed_storage_files`, it will still work but is deprecated:

```yaml
# Old format (deprecated but still supported)
ha_dev_tools:
  security:
    allowed_paths:
      - "configuration.yaml"
      - "automations.yaml"
    
    allowed_storage_files:
      - "lovelace"
      - "input_boolean"
```

**Recommended:** Migrate to the new format:

```yaml
# New format (recommended)
ha_dev_tools:
  security:
    read_paths:
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
      - "/config/.storage/lovelace"
      - "/config/.storage/input_boolean"
    
    write_paths: []
    
    denied_paths:
      - "/config/.storage/auth*"
      - "/config/.storage/core.*"
```

## Best Practices

### Security Best Practices

1. **Start with read-only** - Use `read_paths` by default, only add `write_paths` when necessary
2. **Use specific patterns** - Prefer specific paths over broad wildcards
3. **Always deny sensitive files** - Include authentication and system files in `denied_paths`
4. **Review regularly** - Audit your configuration periodically
5. **Test in development** - Test configuration changes in a development environment first

### Pattern Best Practices

1. **Use glob patterns for flexibility** - Patterns like `*` and `**` make configuration more maintainable
2. **Be specific when possible** - Specific paths are more secure than broad patterns
3. **Document your patterns** - Add comments explaining what each pattern matches
4. **Test pattern matching** - Verify patterns match the files you expect

### Configuration Best Practices

1. **Keep it simple** - Start with minimal configuration and add as needed
2. **Use comments** - Document why each path is included
3. **Version control** - Track configuration changes in git
4. **Separate environments** - Use different configurations for development and production
5. **Monitor logs** - Review security logs for denied access attempts

## Support

For questions or issues with configuration:

- [Issue Tracker](https://github.com/alexlenk/ha-dev-tools/issues)
- [Home Assistant Community Forum](https://community.home-assistant.io/)
- [Documentation](https://github.com/alexlenk/ha-dev-tools)
