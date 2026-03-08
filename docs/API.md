# HA Dev Tools API Documentation

Complete API reference for the HA Dev Tools Home Assistant integration.

## Table of Contents

- [Authentication](#authentication)
- [Base URL](#base-url)
- [Response Formats](#response-formats)
- [Error Handling](#error-handling)
- [File Operations](#file-operations)
- [Metadata Operations](#metadata-operations)
- [Write Operations](#write-operations)
- [Log Operations](#log-operations)
- [Rate Limiting](#rate-limiting)

## Authentication

All API endpoints require authentication using a Home Assistant long-lived access token with administrator privileges.

### Creating an Access Token

1. Navigate to your Home Assistant profile
2. Scroll to "Long-Lived Access Tokens"
3. Click "Create Token"
4. Provide a descriptive name (e.g., "HA Dev Tools API")
5. Copy the generated token

### Using the Token

Include the token in the `Authorization` header of all requests:

```http
Authorization: Bearer YOUR_LONG_LIVED_ACCESS_TOKEN
```

### Example Request

```bash
curl -H "Authorization: Bearer eyJ0eXAiOiJKV1QiLCJhbGc..." \
  http://homeassistant.local:8123/api/management/files
```

## Base URL

The base URL for all API endpoints is your Home Assistant instance URL:

```
http://homeassistant.local:8123
```

Or for HTTPS:

```
https://homeassistant.local:8123
```

## Response Formats

### Success Responses

Successful responses return HTTP status codes in the 2xx range with JSON or plain text content.

**JSON Response Example:**
```json
{
  "success": true,
  "data": { ... }
}
```

**Plain Text Response Example:**
```
homeassistant:
  name: Home
  latitude: 32.87336
```

### Error Responses

Error responses return HTTP status codes in the 4xx or 5xx range with JSON error details.

**Error Response Format:**
```json
{
  "error": "Error type",
  "message": "Human-readable error message",
  "details": { ... }
}
```

## Error Handling

### Common HTTP Status Codes

| Status Code | Meaning | Description |
|-------------|---------|-------------|
| 200 | OK | Request succeeded |
| 400 | Bad Request | Invalid request parameters |
| 401 | Unauthorized | Missing or invalid authentication token |
| 403 | Forbidden | Access denied (insufficient permissions or blocked path) |
| 404 | Not Found | Resource not found |
| 422 | Unprocessable Entity | Validation failed |
| 429 | Too Many Requests | Rate limit exceeded |
| 500 | Internal Server Error | Server error |

### Error Response Examples

**401 Unauthorized:**
```json
{
  "error": "Unauthorized",
  "message": "Invalid authentication token"
}
```

**403 Forbidden:**
```json
{
  "error": "Access denied",
  "message": "File is in denied paths",
  "path": "secrets.yaml",
  "reason": "File is in denied paths"
}
```

**404 Not Found:**
```json
{
  "error": "File not found",
  "message": "The requested file does not exist",
  "path": "nonexistent.yaml"
}
```

**422 Validation Error:**
```json
{
  "error": "Validation failed",
  "message": "Invalid YAML syntax",
  "details": "mapping values are not allowed here\n  in \"<unicode string>\", line 3, column 10"
}
```

**429 Rate Limit:**
```json
{
  "error": "Rate limit exceeded",
  "message": "Too many write operations",
  "limit": "10 writes per minute",
  "retry_after": 45
}
```

## File Operations

### List Files

List all accessible configuration files in a directory.

**Endpoint:** `GET /api/management/files`

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `directory` | string | No | `/config` | Subdirectory to list |

**Request Example:**

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "http://homeassistant.local:8123/api/management/files?directory=packages"
```

**Response Example:**

```json
{
  "files": [
    {
      "name": "configuration.yaml",
      "path": "/config/configuration.yaml",
      "size": 2048,
      "modified": "2026-03-06T10:30:45.123456",
      "type": "file"
    },
    {
      "name": "automations.yaml",
      "path": "/config/automations.yaml",
      "size": 4096,
      "modified": "2026-03-06T11:15:30.654321",
      "type": "file"
    },
    {
      "name": "packages",
      "path": "/config/packages",
      "type": "directory"
    }
  ],
  "directory": "/config"
}
```

**Response Fields:**

- `files`: Array of file/directory objects
  - `name`: File or directory name
  - `path`: Full path from `/config`
  - `size`: File size in bytes (files only)
  - `modified`: ISO 8601 timestamp of last modification (files only)
  - `type`: Either "file" or "directory"
- `directory`: Current directory path

**Error Responses:**

- `403 Forbidden`: Directory is not in allowed paths
- `404 Not Found`: Directory does not exist

### Read File

Read the contents of a configuration file.

**Endpoint:** `GET /api/management/files/{filepath}`

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `filepath` | string | Yes | Relative path from `/config` directory |

**Request Example:**

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://homeassistant.local:8123/api/management/files/configuration.yaml
```

**Response Example:**

```
Content-Type: text/plain
Status: 200 OK

homeassistant:
  name: Home
  latitude: 32.87336
  longitude: -117.22743
  elevation: 430
  unit_system: metric
  time_zone: America/Los_Angeles
```

**Response Format:**

- Content-Type: `text/plain`
- Body: Raw file contents

**Error Responses:**

- `403 Forbidden`: File is not in allowed paths or is in denied paths
- `404 Not Found`: File does not exist

## Metadata Operations

### Get File Metadata

Get metadata for a single file without reading its full content.

**Endpoint:** `GET /api/ha_dev_tools/metadata/{filepath}`

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `filepath` | string | Yes | Relative path from `/config` directory |

**Request Example:**

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  http://homeassistant.local:8123/api/ha_dev_tools/metadata/configuration.yaml
```

**Response Example:**

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

**Response Fields:**

- `path`: Relative path from `/config` directory
- `size`: File size in bytes
- `modified_at`: ISO 8601 timestamp of last modification
- `content_hash`: SHA-256 hash of file content (hex encoded, 64 characters)
- `exists`: Boolean indicating if file exists
- `accessible`: Boolean indicating if file is within allowed paths

**Use Cases:**

- Check if a file has been modified since last download
- Detect version conflicts before uploading changes
- Track file changes over time
- Verify file integrity

**Error Responses:**

- `403 Forbidden`: File is not in allowed paths or is in denied paths
- `404 Not Found`: File does not exist

### Batch Get Metadata

Get metadata for multiple files in a single request.

**Endpoint:** `POST /api/ha_dev_tools/metadata/batch`

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

**Request Fields:**

- `file_paths`: Array of file paths (relative to `/config`), maximum 20 files

**Request Example:**

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
  http://homeassistant.local:8123/api/ha_dev_tools/metadata/batch
```

**Response Example:**

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

**Response Fields:**

- `metadata`: Array of metadata objects (same format as single metadata endpoint)
- `errors`: Array of error objects for files that couldn't be accessed

**Partial Failure Example:**

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

**Error Responses:**

- `400 Bad Request`: Invalid request body or too many files (>20)

## Write Operations

### Write File

Write content to a configuration file. Only available for paths in `write_paths` configuration.

**Endpoint:** `PUT /api/ha_dev_tools/files/{filepath}`

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `filepath` | string | Yes | Relative path from `/config` directory |

**Request Body:**

```json
{
  "content": "homeassistant:\n  name: Home\n  latitude: 32.87336\n",
  "expected_hash": "a3b5c7d9e1f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4",
  "validate_before_write": true
}
```

**Request Fields:**

- `content` (required): File content to write (string)
- `expected_hash` (optional): Expected current hash for conflict detection (string, 64 hex characters)
- `validate_before_write` (optional): Validate YAML syntax before writing (boolean, default: true)

**Request Example:**

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

**Response Example:**

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

**Response Fields:**

- `success`: Boolean indicating success
- `path`: File path that was written
- `metadata`: Updated file metadata (same format as metadata endpoint)
- `config_check`: Configuration validation results
  - `valid`: Boolean indicating if configuration is valid
  - `errors`: Array of validation error messages

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

**Error Responses:**

- `403 Forbidden`: File is not in `write_paths` or is in `denied_paths`
- `409 Conflict`: Version conflict (expected_hash doesn't match current hash)
- `422 Unprocessable Entity`: Validation failed
- `429 Too Many Requests`: Rate limit exceeded

## Log Operations

### Get Core Logs

Retrieve Home Assistant core system logs.

**Endpoint:** `GET /api/management/logs/core`

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `lines` | integer | No | 100 | Number of log lines to return |
| `level` | string | No | All | Filter by log level (DEBUG, INFO, WARNING, ERROR) |
| `search` | string | No | None | Search for specific text in logs |
| `offset` | integer | No | 0 | Offset for pagination |
| `limit` | integer | No | 100 | Maximum number of entries to return |

**Request Example:**

```bash
curl -H "Authorization: Bearer YOUR_TOKEN" \
  "http://homeassistant.local:8123/api/management/logs/core?lines=50&level=ERROR"
```

**Response Example:**

```json
{
  "logs": [
    {
      "timestamp": "2026-03-06T10:30:45.123456",
      "level": "ERROR",
      "message": "Error loading component sensor.template",
      "source": "homeassistant.loader",
      "exception": "ImportError: No module named 'jinja2'"
    },
    {
      "timestamp": "2026-03-06T10:31:12.654321",
      "level": "ERROR",
      "message": "Setup failed for sensor.template",
      "source": "homeassistant.setup"
    }
  ],
  "total_count": 50,
  "source": "core",
  "filters": {
    "level": "ERROR",
    "lines": 50
  }
}
```

**Response Fields:**

- `logs`: Array of log entry objects
  - `timestamp`: ISO 8601 timestamp
  - `level`: Log level (DEBUG, INFO, WARNING, ERROR)
  - `message`: Log message
  - `source`: Logger name
  - `exception`: Exception details (if present)
- `total_count`: Total number of log entries returned
- `source`: Log source (always "core")
- `filters`: Applied filters

**Error Responses:**

- `400 Bad Request`: Invalid query parameters

## Rate Limiting

Write operations are rate-limited to prevent abuse.

### Default Limits

- **10 writes per minute** per user
- **100 writes per hour** per user
- **1000 writes per day** per user

### Rate Limit Headers

Rate limit information is included in response headers:

```http
X-RateLimit-Limit: 10
X-RateLimit-Remaining: 7
X-RateLimit-Reset: 1709726400
```

**Header Descriptions:**

- `X-RateLimit-Limit`: Maximum requests allowed in the current window
- `X-RateLimit-Remaining`: Requests remaining in the current window
- `X-RateLimit-Reset`: Unix timestamp when the rate limit resets

### Rate Limit Exceeded Response

When rate limit is exceeded, a 429 status code is returned:

```json
{
  "error": "Rate limit exceeded",
  "message": "Too many write operations",
  "limit": "10 writes per minute",
  "retry_after": 45
}
```

**Response Fields:**

- `error`: Error type
- `message`: Human-readable message
- `limit`: Rate limit that was exceeded
- `retry_after`: Seconds until rate limit resets

### Configuring Rate Limits

Rate limits can be configured in `configuration.yaml`:

```yaml
ha_dev_tools:
  security:
    rate_limiting:
      enabled: true
      writes_per_minute: 10
      writes_per_hour: 100
      writes_per_day: 1000
```

## Best Practices

### Error Handling

Always check HTTP status codes and handle errors appropriately:

```python
response = requests.get(url, headers=headers)

if response.status_code == 200:
    # Success
    data = response.json()
elif response.status_code == 403:
    # Access denied
    error = response.json()
    print(f"Access denied: {error['message']}")
elif response.status_code == 404:
    # Not found
    print("File not found")
else:
    # Other error
    print(f"Error: {response.status_code}")
```

### Conflict Detection

Always use `expected_hash` when updating files to prevent conflicts:

```python
# 1. Get current metadata
metadata = get_metadata("configuration.yaml")
current_hash = metadata["content_hash"]

# 2. Read and modify content
content = read_file("configuration.yaml")
modified_content = modify(content)

# 3. Write with conflict detection
write_file(
    "configuration.yaml",
    content=modified_content,
    expected_hash=current_hash
)
```

### Batch Operations

Use batch metadata endpoint for efficiency:

```python
# Instead of multiple requests
for file in files:
    metadata = get_metadata(file)  # Multiple requests

# Use batch endpoint
metadata_list = batch_get_metadata(files)  # Single request
```

### Rate Limit Handling

Respect rate limits and implement retry logic:

```python
import time

def write_with_retry(filepath, content, max_retries=3):
    for attempt in range(max_retries):
        response = write_file(filepath, content)
        
        if response.status_code == 200:
            return response.json()
        elif response.status_code == 429:
            # Rate limited
            retry_after = response.json()["retry_after"]
            time.sleep(retry_after)
        else:
            raise Exception(f"Write failed: {response.status_code}")
    
    raise Exception("Max retries exceeded")
```

## Related Documentation

- [Security Guide](SECURITY.md) - Security features and best practices
- [Configuration Examples](CONFIGURATION_EXAMPLES.md) - Configuration examples
- [Troubleshooting Guide](TROUBLESHOOTING.md) - Common issues and solutions
- [Main README](../README.md) - Integration overview and installation
