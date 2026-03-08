# HA Dev Tools Security Guide

Comprehensive security documentation for the HA Dev Tools Home Assistant integration.

## Table of Contents

- [Security Model](#security-model)
- [Authentication](#authentication)
- [Authorization](#authorization)
- [Path Security](#path-security)
- [Write Operations Security](#write-operations-security)
- [Rate Limiting](#rate-limiting)
- [Audit Logging](#audit-logging)
- [Protected Files](#protected-files)
- [Best Practices](#best-practices)
- [Security Checklist](#security-checklist)

## Security Model

HA Dev Tools implements a defense-in-depth security model with multiple layers of protection:

1. **Authentication**: Admin-level access tokens required
2. **Authorization**: Strict allowlist-based path access control
3. **Path Validation**: Prevents directory traversal and path manipulation
4. **Denylist Protection**: Sensitive files always blocked
5. **Rate Limiting**: Prevents abuse of write operations
6. **Audit Logging**: All operations logged for security monitoring
7. **YAML Validation**: Prevents invalid configuration injection
8. **Conflict Detection**: Prevents accidental overwrites

### Security Principles

**Deny by Default**: All access is denied unless explicitly permitted in configuration.

**Least Privilege**: Only grant the minimum necessary permissions for your use case.

**Defense in Depth**: Multiple security layers protect against different attack vectors.

**Audit Trail**: All security-relevant events are logged for monitoring and forensics.

## Authentication

### Access Token Requirements

All API endpoints require a Home Assistant long-lived access token with **administrator privileges**.

**Why Administrator Access?**
- Configuration file access requires elevated permissions
- Log access requires system-level permissions
- Write operations can affect system behavior
- Security model assumes trusted administrators

### Creating a Secure Access Token

1. Navigate to your Home Assistant profile
2. Scroll to "Long-Lived Access Tokens"
3. Click "Create Token"
4. Provide a descriptive name that identifies the purpose:
   - ✅ Good: "HA Dev Tools - Development Laptop"
   - ✅ Good: "HA Dev Tools - CI/CD Pipeline"
   - ❌ Bad: "Token" or "API"
5. Copy the token immediately (it won't be shown again)
6. Store the token securely (see [Token Storage](#token-storage))

### Token Storage

**DO:**
- ✅ Store tokens in environment variables
- ✅ Use secure credential managers (1Password, LastPass, etc.)
- ✅ Use encrypted configuration files
- ✅ Restrict file permissions (chmod 600)
- ✅ Rotate tokens periodically

**DON'T:**
- ❌ Commit tokens to version control
- ❌ Store tokens in plain text files
- ❌ Share tokens between users
- ❌ Use the same token for multiple purposes
- ❌ Store tokens in browser history or logs

### Token Revocation

Revoke tokens immediately if:
- Token is compromised or exposed
- User no longer needs access
- Device is lost or stolen
- Suspicious activity is detected

To revoke a token:
1. Go to your Home Assistant profile
2. Find the token in "Long-Lived Access Tokens"
3. Click the delete/revoke button

## Authorization

### Allowlist-Based Access Control

HA Dev Tools uses a strict allowlist model where only explicitly permitted paths are accessible.

**Configuration Structure:**

```yaml
ha_dev_tools:
  security:
    read_paths:
      - "/config/configuration.yaml"
      - "/config/.storage/lovelace*"
    
    write_paths:
      - "/config/packages/generated/*.yaml"
    
    denied_paths:
      - "/config/secrets.yaml"
```

### Path Types

**Read Paths** (`read_paths`):
- Files can be viewed but not modified
- Recommended for main configuration files
- Lower risk than write paths

**Write Paths** (`write_paths`):
- Files can be both viewed and modified
- Higher risk - use sparingly
- Subject to rate limiting and validation

**Denied Paths** (`denied_paths`):
- Files are completely inaccessible
- Always enforced regardless of other settings
- Protects sensitive files

### Access Decision Logic

When a file is accessed, the following checks are performed in order:

1. **Denied Paths Check**: If file matches `denied_paths`, access is **DENIED**
2. **Write Operation Check**: If operation is write:
   - If file matches `write_paths`, access is **ALLOWED**
   - Otherwise, access is **DENIED**
3. **Read Operation Check**: If operation is read:
   - If file matches `read_paths` or `write_paths`, access is **ALLOWED**
   - Otherwise, access is **DENIED**

**Example:**

```yaml
ha_dev_tools:
  security:
    read_paths:
      - "/config/configuration.yaml"
    write_paths:
      - "/config/packages/generated/*.yaml"
    denied_paths:
      - "/config/secrets.yaml"
```

Access decisions:
- Read `/config/configuration.yaml`: ✅ ALLOWED (in read_paths)
- Write `/config/configuration.yaml`: ❌ DENIED (not in write_paths)
- Read `/config/packages/generated/lights.yaml`: ✅ ALLOWED (in write_paths)
- Write `/config/packages/generated/lights.yaml`: ✅ ALLOWED (in write_paths)
- Read `/config/secrets.yaml`: ❌ DENIED (in denied_paths)
- Write `/config/secrets.yaml`: ❌ DENIED (in denied_paths)
- Read `/config/unknown.yaml`: ❌ DENIED (not in any allowed paths)

## Path Security

### Path Validation

All file paths undergo strict validation to prevent security vulnerabilities:

1. **Normalization**: Paths are normalized to resolve `.` and `..` components
2. **Absolute Path Check**: Paths must be within `/config` directory
3. **Traversal Prevention**: Path traversal attempts (`../`) are blocked
4. **Symlink Resolution**: Symlinks are resolved and validated
5. **Null Byte Check**: Null bytes in paths are rejected

### Directory Traversal Prevention

The integration prevents directory traversal attacks:

**Blocked Attempts:**
```
/config/../etc/passwd          → BLOCKED
/config/./../../secrets.yaml   → BLOCKED
/config/%2e%2e/secrets.yaml    → BLOCKED (URL encoded)
```

**Allowed Paths:**
```
/config/configuration.yaml     → ALLOWED (if in read_paths)
/config/packages/lights.yaml   → ALLOWED (if in read_paths)
```

### Glob Pattern Security

Glob patterns are powerful but must be used carefully:

**Safe Patterns:**
```yaml
# Specific file types
- "/config/*.yaml"

# Specific subdirectories
- "/config/packages/**/*.yaml"

# Specific prefixes
- "/config/.storage/lovelace*"
```

**Dangerous Patterns:**
```yaml
# Too broad - matches everything
- "/config/**/*"

# Could match sensitive files
- "/config/**/*.yaml"  # Includes secrets.yaml!

# Overly permissive
- "/config/*"  # Matches all files in root
```

**Best Practice:** Use the most specific patterns possible.

## Write Operations Security

### Write Operation Requirements

Write operations have additional security requirements beyond read operations:

1. **Explicit Configuration**: Path must be in `write_paths`
2. **YAML Validation**: YAML files are validated before writing
3. **Atomic Writes**: Files are written atomically to prevent corruption
4. **Automatic Backups**: Original files are backed up before modification
5. **Configuration Checks**: Config files trigger HA configuration validation
6. **Rate Limiting**: Write operations are rate-limited
7. **Audit Logging**: All write operations are logged

### YAML Validation

All YAML files are validated before writing to prevent:
- Syntax errors that break Home Assistant
- Invalid configuration that causes startup failures
- Malformed data structures

**Validation Process:**

1. Parse YAML content
2. Check for syntax errors
3. Validate structure (if schema available)
4. Return validation results

**Example Validation Error:**

```json
{
  "error": "Validation failed",
  "message": "Invalid YAML syntax",
  "details": "mapping values are not allowed here\n  in \"<unicode string>\", line 3, column 10"
}
```

### Atomic Writes

Files are written atomically to prevent corruption:

1. Write content to temporary file
2. Validate temporary file
3. Create backup of original file
4. Rename temporary file to target (atomic operation)
5. Verify write succeeded

If any step fails, the original file remains unchanged.

### Automatic Backups

Before modifying a file, a backup is created:

**Backup Location:** `/config/.ha_dev_tools_backups/`

**Backup Naming:** `{filename}.{timestamp}.backup`

**Example:**
```
Original: /config/configuration.yaml
Backup:   /config/.ha_dev_tools_backups/configuration.yaml.20260306_120000.backup
```

**Backup Retention:** Last 10 backups per file are kept.

### Conflict Detection

Use `expected_hash` to prevent overwriting newer versions:

**Workflow:**

1. Read file and get metadata (including hash)
2. Modify content locally
3. Write with `expected_hash` parameter
4. If hash doesn't match, conflict is detected

**Example:**

```python
# 1. Get current state
metadata = get_metadata("configuration.yaml")
current_hash = metadata["content_hash"]

# 2. Modify content
content = read_file("configuration.yaml")
modified = modify(content)

# 3. Write with conflict detection
try:
    write_file(
        "configuration.yaml",
        content=modified,
        expected_hash=current_hash
    )
except ConflictError:
    # File was modified by someone else
    # Handle conflict (merge, overwrite, abort)
    pass
```

## Rate Limiting

### Default Rate Limits

Write operations are rate-limited to prevent abuse:

- **10 writes per minute** per user
- **100 writes per hour** per user
- **1000 writes per day** per user

### Rate Limit Configuration

Configure rate limits in `configuration.yaml`:

```yaml
ha_dev_tools:
  security:
    rate_limiting:
      enabled: true
      writes_per_minute: 10
      writes_per_hour: 100
      writes_per_day: 1000
```

### Disabling Rate Limiting

**NOT RECOMMENDED** for production:

```yaml
ha_dev_tools:
  security:
    rate_limiting:
      enabled: false
```

Only disable rate limiting in:
- Development environments
- Testing environments
- Controlled environments with other rate limiting mechanisms

### Rate Limit Response

When rate limit is exceeded:

```json
{
  "error": "Rate limit exceeded",
  "message": "Too many write operations",
  "limit": "10 writes per minute",
  "retry_after": 45
}
```

**Response Headers:**

```http
HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1709726400
Retry-After: 45
```

### Rate Limit Best Practices

**DO:**
- ✅ Implement exponential backoff
- ✅ Respect `Retry-After` header
- ✅ Batch operations when possible
- ✅ Cache data to reduce requests
- ✅ Monitor rate limit headers

**DON'T:**
- ❌ Retry immediately after 429 response
- ❌ Ignore rate limit headers
- ❌ Disable rate limiting in production
- ❌ Use multiple tokens to bypass limits

## Audit Logging

### Logged Events

All security-relevant events are logged:

**Access Events:**
- File read operations
- File write operations
- Metadata requests
- Directory listings

**Security Events:**
- Access denied (403)
- Path traversal attempts
- Invalid authentication
- Rate limit violations

**Validation Events:**
- YAML validation failures
- Configuration check failures
- Conflict detection

### Log Format

Logs are written to Home Assistant system logs with source `ha_dev_tools.security`:

**Example Log Entries:**

```
2026-03-06 10:30:45 INFO (MainThread) [ha_dev_tools.security] 
  Write operation: user=admin, path=/config/packages/generated/lights.yaml, 
  operation=update, status=success, validated=true

2026-03-06 10:31:12 WARNING (MainThread) [ha_dev_tools.security] 
  Write operation denied: user=admin, path=/config/secrets.yaml, 
  reason=denied_path

2026-03-06 10:32:05 ERROR (MainThread) [ha_dev_tools.security] 
  Write operation failed: user=admin, path=/config/automations.yaml, 
  reason=validation_failed, error=invalid YAML syntax at line 15

2026-03-06 10:33:20 WARNING (MainThread) [ha_dev_tools.security] 
  Path traversal attempt: user=admin, path=/config/../etc/passwd, 
  blocked=true

2026-03-06 10:34:45 WARNING (MainThread) [ha_dev_tools.security] 
  Rate limit exceeded: user=admin, limit=writes_per_minute, 
  current=11, max=10
```

### Log Monitoring

Monitor logs for security events:

**Critical Events to Monitor:**
- Multiple access denied events
- Path traversal attempts
- Rate limit violations
- Validation failures
- Unexpected write operations

**Monitoring Tools:**
- Home Assistant log viewer
- External log aggregation (Splunk, ELK, etc.)
- Security information and event management (SIEM)

## Protected Files

### Always Denied Files

The following files are **always denied** regardless of configuration:

**Authentication & Security:**
- `/config/secrets.yaml` - Sensitive credentials
- `/config/.storage/auth*` - Authentication data
- `/config/.storage/core.restore_state` - State data
- `/config/.cloud` - Cloud integration data
- `/config/.uuid` - System identifier

**System Files:**
- `/config/.HA_VERSION` - Version file
- `/config/home-assistant.log` - Core log file (use log API)
- `/config/home-assistant_v2.db` - Database file
- `/config/*.db` - All database files
- `/config/*.db-shm` - Database shared memory
- `/config/*.db-wal` - Database write-ahead log

**Why These Files Are Protected:**

- **secrets.yaml**: Contains passwords, API keys, tokens
- **auth files**: User credentials and session data
- **Database files**: Direct access could corrupt data
- **System files**: Modification could break Home Assistant

### Custom Denied Paths

Add additional denied paths in configuration:

```yaml
ha_dev_tools:
  security:
    denied_paths:
      # Add to default denied paths
      - "/config/custom_sensitive_file.yaml"
      - "/config/private/**/*"
```

## Best Practices

### Configuration Best Practices

**DO:**
- ✅ Use the most restrictive configuration possible
- ✅ Enable write access only for specific generated/temporary files
- ✅ Use specific glob patterns instead of wildcards
- ✅ Keep rate limiting enabled in production
- ✅ Regularly review and update allowed paths
- ✅ Document why each path is allowed

**DON'T:**
- ❌ Enable write access to `configuration.yaml` unless absolutely necessary
- ❌ Use overly broad glob patterns like `/config/**/*`
- ❌ Disable rate limiting in production
- ❌ Grant access to `.storage/` files
- ❌ Allow write access to files managed through HA UI

### Token Management Best Practices

**DO:**
- ✅ Create separate tokens for different purposes
- ✅ Use descriptive token names
- ✅ Rotate tokens periodically (every 90 days)
- ✅ Revoke unused tokens immediately
- ✅ Store tokens securely
- ✅ Monitor token usage

**DON'T:**
- ❌ Share tokens between users or systems
- ❌ Commit tokens to version control
- ❌ Use the same token for development and production
- ❌ Store tokens in plain text
- ❌ Leave tokens active after they're no longer needed

### Operational Best Practices

**DO:**
- ✅ Monitor audit logs regularly
- ✅ Set up alerts for suspicious activity
- ✅ Test configuration changes in development first
- ✅ Back up configuration before enabling write operations
- ✅ Use conflict detection for critical files
- ✅ Validate YAML before writing

**DON'T:**
- ❌ Ignore security warnings in logs
- ❌ Disable security features for convenience
- ❌ Skip validation to save time
- ❌ Ignore rate limit violations
- ❌ Grant excessive permissions "just in case"

### Development vs Production

**Development Environment:**
```yaml
ha_dev_tools:
  security:
    read_paths:
      - "/config/**/*.yaml"  # Broader access for development
    write_paths:
      - "/config/test_configs/**/*"
    rate_limiting:
      writes_per_minute: 50  # Higher limits for testing
```

**Production Environment:**
```yaml
ha_dev_tools:
  security:
    read_paths:
      - "/config/configuration.yaml"
      - "/config/automations.yaml"
      - "/config/.storage/lovelace*"
    write_paths: []  # No write access in production
    rate_limiting:
      writes_per_minute: 10  # Strict limits
```

## Security Checklist

### Initial Setup

- [ ] Create dedicated admin access token
- [ ] Store token securely (not in version control)
- [ ] Configure minimal `read_paths` for your use case
- [ ] Leave `write_paths` empty unless needed
- [ ] Review default `denied_paths`
- [ ] Enable rate limiting
- [ ] Test configuration in development first

### Regular Maintenance

- [ ] Review audit logs weekly
- [ ] Rotate access tokens every 90 days
- [ ] Review and update allowed paths monthly
- [ ] Check for unused tokens and revoke them
- [ ] Monitor rate limit violations
- [ ] Update integration to latest version
- [ ] Review security best practices

### Before Enabling Write Operations

- [ ] Backup all configuration files
- [ ] Test write operations in development
- [ ] Configure specific `write_paths` (not wildcards)
- [ ] Enable YAML validation
- [ ] Set up monitoring for write operations
- [ ] Document why write access is needed
- [ ] Review rate limiting configuration

### Incident Response

If you suspect a security issue:

1. **Immediate Actions:**
   - Revoke all HA Dev Tools access tokens
   - Review audit logs for suspicious activity
   - Check for unauthorized file modifications
   - Restore from backup if needed

2. **Investigation:**
   - Identify what was accessed/modified
   - Determine how access was obtained
   - Check for other compromised credentials

3. **Remediation:**
   - Create new access tokens
   - Update security configuration
   - Implement additional monitoring
   - Document incident and lessons learned

4. **Prevention:**
   - Review and tighten security configuration
   - Implement additional access controls
   - Increase monitoring and alerting
   - Train users on security best practices

## Related Documentation

- [API Documentation](API.md) - Complete API reference
- [Configuration Examples](CONFIGURATION_EXAMPLES.md) - Configuration examples
- [Troubleshooting Guide](TROUBLESHOOTING.md) - Common issues and solutions
- [Main README](../README.md) - Integration overview and installation
