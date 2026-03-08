# GitHub Release Instructions for ha-dev-tools v1.0.0

## Status

- ✅ Version 1.0.0 tagged
- ✅ Tag pushed to GitHub
- ⏳ GitHub release creation (requires manual step)
- ⏳ HACS discovery verification

## Creating the GitHub Release

### Step 1: Navigate to GitHub Releases

1. Go to https://github.com/alexlenk/ha-dev-tools/releases
2. Click "Draft a new release"

### Step 2: Configure the Release

**Tag**: Select `v1.0.0` from the dropdown (already pushed)

**Release Title**: `HA Dev Tools v1.0.0 - Initial Release`

**Description**: Copy the content from `RELEASE_NOTES_v1.0.0.md`

**Options**:
- ✅ Set as the latest release
- ✅ Create a discussion for this release (optional)
- ❌ Do NOT mark as pre-release

### Step 3: Publish the Release

Click "Publish release"

## Verifying HACS Discovery

After creating the release, verify that HACS can discover the integration:

### Method 1: HACS Validation Action

The repository should have a GitHub Action that validates HACS compatibility. Check:
- https://github.com/alexlenk/ha-dev-tools/actions

### Method 2: Manual HACS Installation Test

1. Open Home Assistant
2. Go to HACS → Integrations
3. Click the three dots → Custom repositories
4. Add: `https://github.com/alexlenk/ha-dev-tools`
5. Category: Integration
6. Click "Add"
7. Search for "HA Dev Tools"
8. Verify it appears in the list
9. Try installing it

### Method 3: HACS Validation Tool

Run the HACS validation locally:

```bash
cd release/ha-dev-tools

# Using Docker
docker run --rm -v $(pwd):/github/workspace ghcr.io/hacs/action:main

# Or using the HACS CLI (if installed)
hacs validate
```

## Expected HACS Structure

HACS requires this structure (already in place):

```
ha-dev-tools/
├── custom_components/
│   └── ha_dev_tools/
│       ├── __init__.py
│       ├── manifest.json
│       ├── config_flow.py
│       └── ... (other files)
├── hacs.json
├── README.md
└── LICENSE
```

## Verification Checklist

After creating the release:

- [ ] Release appears at https://github.com/alexlenk/ha-dev-tools/releases
- [ ] Tag v1.0.0 is visible in the tags list
- [ ] Release notes are properly formatted
- [ ] HACS validation passes (check Actions tab)
- [ ] Integration can be added via HACS custom repositories
- [ ] Integration installs successfully in Home Assistant
- [ ] Integration loads without errors in HA logs

## Troubleshooting

### Issue: HACS can't find the repository

**Solution**: 
- Ensure the repository is public
- Verify hacs.json exists and is valid
- Check that manifest.json has correct structure
- Wait a few minutes for GitHub to index the release

### Issue: HACS validation fails

**Solution**:
- Check the Actions tab for validation errors
- Verify manifest.json has all required fields
- Ensure custom_components/ha_dev_tools/ structure is correct
- Check that version in manifest.json matches tag

### Issue: Integration doesn't load in Home Assistant

**Solution**:
- Check Home Assistant logs for errors
- Verify Python version compatibility (3.12+)
- Ensure all required files are present
- Test with a fresh Home Assistant installation

## Next Steps

After successful release:

1. Update the main README with installation instructions
2. Announce the release in Home Assistant community forums
3. Update the Kiro power to reference this release
4. Monitor GitHub issues for user feedback
5. Prepare for future releases with changelog updates
