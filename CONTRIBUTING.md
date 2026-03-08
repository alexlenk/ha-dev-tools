# Contributing to HA Dev Tools

Thank you for your interest in contributing to HA Dev Tools! This document provides guidelines and instructions for contributing to the Home Assistant integration.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Development Workflow](#development-workflow)
- [Testing](#testing)
- [Code Style](#code-style)
- [Submitting Changes](#submitting-changes)
- [Reporting Issues](#reporting-issues)

## Code of Conduct

This project follows the [Home Assistant Code of Conduct](https://www.home-assistant.io/code_of_conduct/). By participating, you are expected to uphold this code.

## Getting Started

### Prerequisites

- Python 3.12 or later
- Home Assistant 2024.1.0 or later
- Git
- Basic understanding of Home Assistant custom integrations
- Familiarity with async Python programming

### Project Structure

```
ha-dev-tools/
├── custom_components/
│   └── ha_dev_tools/          # Integration source code
│       ├── __init__.py        # Integration setup
│       ├── manifest.json      # Integration metadata
│       ├── config_flow.py     # Configuration flow
│       ├── const.py           # Constants
│       ├── api.py             # REST API endpoints
│       ├── file_manager.py    # File operations
│       ├── log_manager.py     # Log operations
│       └── security.py        # Security manager
├── tests/                     # Test suite
│   ├── unit/                  # Unit tests
│   ├── integration/           # Integration tests
│   └── property/              # Property-based tests
├── docs/                      # Documentation
├── .github/                   # GitHub workflows
├── README.md                  # Main documentation
├── CONTRIBUTING.md            # This file
└── LICENSE                    # MIT License
```

## Development Setup

### 1. Fork and Clone

Fork the repository on GitHub and clone your fork:

```bash
git clone https://github.com/YOUR_USERNAME/ha-dev-tools.git
cd ha-dev-tools
```

### 2. Set Up Python Environment

Create a virtual environment with Python 3.12+:

```bash
# Create virtual environment
python3.12 -m venv .venv

# Activate virtual environment
source .venv/bin/activate  # On macOS/Linux
# or
.venv\Scripts\activate  # On Windows
```

### 3. Install Dependencies

Install development dependencies:

```bash
pip install -r requirements-test.txt
```

This installs:
- Home Assistant
- pytest and pytest-asyncio
- pytest-homeassistant-custom-component
- Hypothesis (for property-based testing)
- Code quality tools (ruff, black, mypy)

### 4. Set Up Home Assistant for Testing

For integration testing, you need a Home Assistant instance:

**Option A: Development Container (Recommended)**

Use the Home Assistant development container:

```bash
# Install Home Assistant in development mode
pip install homeassistant

# Create a test configuration directory
mkdir -p ha-dev-test/config
```

**Option B: Existing Home Assistant Instance**

If you have an existing Home Assistant instance, you can test against it:

1. Copy the integration to your Home Assistant `custom_components` directory
2. Configure the integration in `configuration.yaml`
3. Restart Home Assistant

### 5. Configure the Integration

Create a test configuration in `ha-dev-test/config/configuration.yaml`:

```yaml
# Minimal Home Assistant configuration
homeassistant:
  name: Test
  latitude: 32.87336
  longitude: -117.22743
  elevation: 430
  unit_system: metric
  time_zone: America/Los_Angeles

# HA Dev Tools configuration
ha_dev_tools:
  security:
    read_paths:
      - "/config/**/*.yaml"
    write_paths:
      - "/config/test_configs/**/*"
    denied_paths:
      - "/config/secrets.yaml"
```

## Development Workflow

### 1. Create a Feature Branch

```bash
git checkout -b feature/your-feature-name
```

Use descriptive branch names:
- `feature/add-template-validation`
- `fix/file-permission-error`
- `docs/update-api-documentation`

### 2. Make Your Changes

Follow these guidelines:

- Write clear, self-documenting code
- Add docstrings to all functions and classes
- Follow the existing code style
- Add tests for new functionality
- Update documentation as needed

### 3. Test Your Changes

Run the test suite to ensure your changes don't break existing functionality:

```bash
# Run all tests
PYTHONPATH=. python -m pytest tests/ -v

# Run specific test file
PYTHONPATH=. python -m pytest tests/test_file_manager.py -v

# Run with coverage
PYTHONPATH=. python -m pytest tests/ --cov=custom_components/ha_dev_tools
```

### 4. Commit Your Changes

Write clear, descriptive commit messages:

```bash
git add .
git commit -m "Add template validation to file write operations"
```

**Good commit messages:**
- "Add rate limiting to write operations"
- "Fix path traversal vulnerability in file manager"
- "Update API documentation with new endpoints"

**Bad commit messages:**
- "Update"
- "Fix bug"
- "Changes"

### 5. Push and Create Pull Request

```bash
git push origin feature/your-feature-name
```

Then create a pull request on GitHub with:
- Clear description of changes
- Reference to related issues
- Screenshots (if UI changes)
- Test results

## Testing

### Test Types

**Unit Tests** (`tests/unit/`):
- Test individual functions and classes
- Mock external dependencies
- Fast execution

**Integration Tests** (`tests/integration/`):
- Test integration with Home Assistant
- Use Home Assistant test fixtures
- Test API endpoints

**Property-Based Tests** (`tests/property/`):
- Test correctness properties
- Use Hypothesis for test generation
- Find edge cases automatically

### Running Tests

**All Tests:**
```bash
PYTHONPATH=. python -m pytest tests/ -v
```

**Unit Tests Only:**
```bash
PYTHONPATH=. python -m pytest tests/unit/ -v
```

**Integration Tests Only:**
```bash
PYTHONPATH=. python -m pytest tests/integration/ -v
```

**Property-Based Tests:**
```bash
PYTHONPATH=. python -m pytest tests/property/ -v --hypothesis-show-statistics
```

**With Coverage:**
```bash
PYTHONPATH=. python -m pytest tests/ --cov=custom_components/ha_dev_tools --cov-report=html
```

### Writing Tests

**Unit Test Example:**

```python
import pytest
from custom_components.ha_dev_tools.file_manager import FileManager

@pytest.mark.asyncio
async def test_read_file(mock_hass):
    """Test reading a configuration file."""
    file_manager = FileManager(mock_hass)
    
    content = await file_manager.read_file("configuration.yaml")
    
    assert content is not None
    assert "homeassistant:" in content
```

**Property-Based Test Example:**

```python
from hypothesis import given, strategies as st
import pytest

@given(file_path=st.text())
def test_path_validation_property(file_path):
    """Test that path validation never allows traversal."""
    from custom_components.ha_dev_tools.security import SecurityManager
    
    security = SecurityManager()
    
    # Property: Path validation should never allow traversal
    if ".." in file_path:
        assert not security.is_path_safe(file_path)
```

### Test Requirements

All contributions must:
- Include tests for new functionality
- Maintain or improve code coverage
- Pass all existing tests
- Follow testing best practices

## Code Style

### Python Style Guide

Follow [PEP 8](https://pep8.org/) and Home Assistant's [style guidelines](https://developers.home-assistant.io/docs/development_guidelines):

- Use 4 spaces for indentation
- Maximum line length: 88 characters (Black default)
- Use type hints for function parameters and return values
- Write docstrings for all public functions and classes

### Code Formatting

Use Black for code formatting:

```bash
black custom_components/ha_dev_tools/
```

### Linting

Use Ruff for linting:

```bash
ruff check custom_components/ha_dev_tools/
```

### Type Checking

Use mypy for type checking:

```bash
mypy custom_components/ha_dev_tools/
```

### Pre-Commit Checks

Before committing, run:

```bash
# Format code
black custom_components/ha_dev_tools/ tests/

# Check linting
ruff check custom_components/ha_dev_tools/ tests/

# Run tests
PYTHONPATH=. python -m pytest tests/ -v
```

## Submitting Changes

### Pull Request Process

1. **Update Documentation**: Ensure documentation is updated for any changes
2. **Add Tests**: Include tests for new functionality
3. **Run Tests**: Ensure all tests pass
4. **Update Changelog**: Add entry to CHANGELOG.md (if applicable)
5. **Create Pull Request**: Submit PR with clear description

### Pull Request Template

```markdown
## Description
Brief description of changes

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Breaking change
- [ ] Documentation update

## Testing
- [ ] Unit tests added/updated
- [ ] Integration tests added/updated
- [ ] Property-based tests added/updated
- [ ] All tests pass

## Checklist
- [ ] Code follows style guidelines
- [ ] Documentation updated
- [ ] Tests added/updated
- [ ] Changelog updated (if applicable)
```

### Review Process

1. Maintainers will review your pull request
2. Address any feedback or requested changes
3. Once approved, your PR will be merged
4. Your contribution will be included in the next release

## Reporting Issues

### Bug Reports

When reporting bugs, include:

- **Description**: Clear description of the bug
- **Steps to Reproduce**: Detailed steps to reproduce the issue
- **Expected Behavior**: What you expected to happen
- **Actual Behavior**: What actually happened
- **Environment**:
  - Home Assistant version
  - HA Dev Tools version
  - Python version
  - Operating system
- **Logs**: Relevant log entries
- **Configuration**: Relevant configuration (redact sensitive info)

### Feature Requests

When requesting features, include:

- **Description**: Clear description of the feature
- **Use Case**: Why this feature would be useful
- **Proposed Solution**: How you envision it working
- **Alternatives**: Alternative solutions you've considered

### Security Issues

**DO NOT** report security issues publicly. Instead:

1. Email security concerns to [security contact]
2. Include detailed description of the vulnerability
3. Wait for response before public disclosure

## Development Tips

### Debugging

**Enable Debug Logging:**

Add to `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.ha_dev_tools: debug
```

**Use Python Debugger:**

```python
import pdb; pdb.set_trace()
```

**Check Logs:**

```bash
tail -f ha-dev-test/config/home-assistant.log
```

### Testing with Real Home Assistant

1. Copy integration to Home Assistant:
   ```bash
   cp -r custom_components/ha_dev_tools /path/to/homeassistant/custom_components/
   ```

2. Restart Home Assistant

3. Check logs for errors

4. Test API endpoints:
   ```bash
   curl -H "Authorization: Bearer YOUR_TOKEN" \
     http://homeassistant.local:8123/api/management/files
   ```

### Common Issues

**Import Errors:**
- Ensure PYTHONPATH is set correctly
- Check that all dependencies are installed

**Test Failures:**
- Check that Home Assistant is installed
- Verify Python version (3.12+)
- Review test output for specific errors

**Integration Not Loading:**
- Check manifest.json is valid
- Verify all required files are present
- Review Home Assistant logs

## Resources

### Documentation

- [Home Assistant Developer Docs](https://developers.home-assistant.io/)
- [Home Assistant Architecture](https://developers.home-assistant.io/docs/architecture_index)
- [Integration Development](https://developers.home-assistant.io/docs/creating_integration_manifest)

### Tools

- [pytest-homeassistant-custom-component](https://github.com/MatthewFlamm/pytest-homeassistant-custom-component)
- [Hypothesis](https://hypothesis.readthedocs.io/)
- [Black](https://black.readthedocs.io/)
- [Ruff](https://docs.astral.sh/ruff/)

### Community

- [Home Assistant Community Forum](https://community.home-assistant.io/)
- [Home Assistant Discord](https://discord.gg/home-assistant)
- [GitHub Discussions](https://github.com/your-username/ha-dev-tools/discussions)

## License

By contributing to HA Dev Tools, you agree that your contributions will be licensed under the MIT License.

## Questions?

If you have questions about contributing:

1. Check existing documentation
2. Search closed issues and pull requests
3. Ask in GitHub Discussions
4. Contact maintainers

Thank you for contributing to HA Dev Tools! 🎉
