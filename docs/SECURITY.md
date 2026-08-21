# Security

This integration exposes tools that can read and write your Home Assistant
configuration - automations, dashboards, helpers, arbitrary files within an
allowlist, log contents, Supervisor add-on logs. That's a lot more power
than a typical integration, and it deserves a threat model spelled out
explicitly rather than assumed. Everything below was verified by reading the
actual Home Assistant and `mcp_server` source, not assumed from how a
"normal" integration behaves.

## Why this needed more than "require admin"

Home Assistant's own token model has **no concept of a scoped credential**.
`homeassistant/auth/models.py`'s `RefreshToken` - the thing every access
token, whether a mobile app's, a browser session's, or a long-lived token
pasted into some other script years ago, ultimately derives from - carries
no scope field at all. Whatever `user.is_admin` resolves to for a given
user, every single token that user holds is exactly that powerful. Home
Assistant has no native way to mint a credential that can do less than
everything that user's account can do.

Under a stock Home Assistant install, that's usually fine, because the
ceiling of "everything an admin account can do" through the web/API layer is
itself bounded: entity control, service calls, config UI edits. Raw file
access - the kind that lets you write an automation with a `shell_command`
or `rest_command` action, i.e. a path to arbitrary execution - normally sits
behind a **separate credential domain entirely**: SSH, the Terminal add-on,
or Samba, none of which share any identity system with a Home Assistant
user account. Compromising someone's HA login has never meant compromising
their SSH access, and vice versa.

This integration collapses that separation. Reachable over MCP, `dev_tools`
puts file read/write and the `shell_command`/`rest_command`-via-automation
path behind the *same* credential as normal Home Assistant API access.
Requiring `is_admin` alone doesn't restore the separation - it just means
the credential that already grants this access is the same one embedded in
that admin's phone app, browser session, and any other long-lived token
they've ever created and forgotten about. None of those were ever managed
with "this is as sensitive as an SSH key" in mind.

There's also a narrower, more mechanical gap: `mcp_server`'s own admin check
(`homeassistant/components/mcp_server/http.py`) only covers the explicit
`/api/mcp/<api_id>` URL. The bare `/api/mcp` endpoint serves whatever APIs a
`mcp_server` config entry lists, with **no admin check at all** - just
ordinary Home Assistant authentication. If `dev_tools` ever ends up in that
entry's default API list (a plain multi-select in `mcp_server`'s own config
flow, with no warning distinguishing a narrow API like Assist from a broad
one like this), any authenticated non-admin account - a restricted
household member, a guest - reaches it through that endpoint with the admin
check bypassed entirely.

## The two gates

Every tool except the diagnostic `dev_tools_ping` requires both of the
following before it does anything (`custom_components/ha_dev_tools/access_control.py`,
enforced by a shared `GatedTool` base class so no individual tool can
accidentally skip it):

### 1. Proof of recent out-of-band access (the "arm file")

A specific file - by default `/config/.storage/ha_dev_tools.armed` - must
exist and be recent. This integration is denylisted from ever writing that
file's content itself (see `const.py`'s `DEFAULT_DENYLIST`), and the code
path that checks it never routes through the general file-write machinery
at all. Only something with real filesystem access - SSH, the Terminal
add-on, Samba - can create it. This restores the separate-credential-domain
property described above: a leaked ordinary Home Assistant token, on its
own, cannot arm `dev_tools`.

**Arming:**

```bash
date +%s > /config/.storage/ha_dev_tools.armed
```

**How long it stays armed:**

- Up to **30 minutes idle**. Every successful tool call bumps the file's
  modification time, extending the window - this is what lets a real
  working session continue without you re-arming by hand every half hour.
- A **hard cap of 4 hours** from the moment it was first created, regardless
  of activity. This is read from the file's *content* (the timestamp you
  wrote when arming it), which `dev_tools` never rewrites - only its mtime
  is touched on each use. If `dev_tools` could rewrite the content too, a
  busy session could reset its own ceiling indefinitely; keeping that one
  field permanently outside its own reach is what makes the cap real.

A background task removes an expired arm file on a 5-minute cycle, purely
for tidiness. It is **never what enforces expiry** - every gated call
re-derives armed/expired state directly from the file on disk, so a missed
cleanup tick (a Home Assistant restart, for instance) can never leave
`dev_tools` armed longer than intended.

**On Home Assistant OS/Supervised**, the `homeassistant` container's
filesystem is a constrained bind-mount - a location genuinely outside
`/config`, reachable only via host-level SSH, would be unreachable from
inside this integration's own process even under a worst-case scenario (a
malicious automation achieving code execution inside the container). That
would be true credential-domain separation, matching SSH exactly. On a bare
Core/venv/Docker install, where "this process" and "SSH into the box" are
the same OS user, the separation is weaker - but the deliberate arm/disarm
ritual and the time-bounding still apply.

### 2. A genuine admin account

The calling user - resolved from the MCP request's real authenticated
context, never a synthetic bypass - must have `is_admin` set. This is
checked independently of `mcp_server`'s own gate, closing the bare-`/api/mcp`
endpoint gap described above regardless of how `mcp_server` happens to be
configured.

## Path allowlist (file-touching tools)

Tools that touch files on disk (`write_automation`, and the underlying
`FileManager` more generally) are further bounded by `SecurityManager`'s
allowlist/denylist of paths - independent of, and in addition to, the two
gates above. Sensitive files (`secrets.yaml`, auth storage, the arm file
itself) are permanently denylisted regardless of configuration. See
[CONFIGURATION_EXAMPLES.md](CONFIGURATION_EXAMPLES.md) to customize the
allowlist; sane defaults apply if you don't.

## What this does *not* do

- It does not sandbox this integration's own code. Home Assistant gives
  custom integrations no process isolation - `ha_dev_tools` runs with the
  same privileges as Home Assistant itself, same as any other integration.
  The gates above control *when* and *by whom* the tools can be invoked, not
  what they're capable of once invoked.
- It does not encrypt or specially protect the arm file's timestamp
  content - it's not a secret, it's a proof-of-recent-physical-access
  marker. Anyone who can read `/config/.storage/` can see it exists; that's
  fine, since reading it grants nothing.
- It does not replace normal Home Assistant hardening: keep this instance
  off the open internet, use a real password/MFA on admin accounts, and
  don't add `dev_tools` to `mcp_server`'s default API list unless you've
  read the section above and understand what that means.
