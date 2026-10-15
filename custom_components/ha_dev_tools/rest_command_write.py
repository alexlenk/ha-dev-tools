"""Writing `rest_command:` entries in packages (issue #168).

rest_command was read-only (#73) on the same grounds as shell_command; the
owner's evaluation on #168 put it in a different risk class: it can't run
anything on the host, and this tool only writes config - running a command
still takes write_automation/write_script plus trigger_automation, each
confirm-gated, and there is no generic service-call tool (#76). What's new
is arbitrary outbound HTTP to a new destination, with a payload that can
template any state. So, per the owner's decisions on #168:

- the URL's scheme and host must be literal - templates only in the path,
  query, payload and headers - so the target is fixed and visible;
- a host no existing rest_command uses needs allow_new_host, and the
  preview says whether it's a private/LAN or a public address;
- a literal credential (a credential-shaped header, username, password, a
  password in the URL, a bearer token) is refused - `!secret` only - and
  never echoed;
- `verify_ssl: false` needs allow_insecure_tls when it's new (an entry
  that already had it keeps it);
- packages only - configuration.yaml stays read-only.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from io import StringIO
from typing import Any
from urllib.parse import urlsplit

from homeassistant.core import HomeAssistant
from ruamel.yaml.comments import CommentedMap

from . import mirror_secrets
from .file_manager import package_file
from .rest_command_manager import (
    DEFAULT_CONFIG_FILE,
    REST_COMMAND_KEY,
    RestCommandManager,
    _load_yaml,
    _new_yaml,
)
from .yaml_style import (
    merge_preserving_style,
    quote_ambiguous_scalars,
    spliced_or_full,
    surgical_edit,
    tag_references,
    to_json_safe,
)

_SCHEME_HOST = re.compile(r"\s*([^/]*?)://([^/?#]*)")
_LAN_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa")
_DEFAULT_TIMEOUT = 10


class RestCommandWriteError(ValueError):
    """A rest_command write that's refused - nothing changed."""


@dataclass(slots=True)
class RestCommandPlan:
    rest_command_id: str
    file_path: str
    content_before: str
    content_after: str
    report: dict[str, Any]


def target_host(url: Any) -> str:
    """The URL's host, refusing a templated or missing scheme/host."""
    if not isinstance(url, str) or not url.strip():
        raise RestCommandWriteError("url is required")
    match = _SCHEME_HOST.match(url)
    if match is None:
        raise RestCommandWriteError(
            "url's scheme and host must be written out (http(s)://host/...) - "
            "a template may only build the path, query, payload or headers"
        )
    if any(mark in match.group(0) for mark in ("{", "}", "%")):
        raise RestCommandWriteError(
            "url's scheme and host can't be a template - only the path, query, "
            "payload or headers can"
        )
    host = urlsplit(url.strip()).hostname
    if not host:
        raise RestCommandWriteError(f"url has no host: {match.group(0)!r}")
    return host.lower()


def host_class(host: str) -> str:
    """'private' / 'public' for an IP address, 'lan (by name)' / 'public
    (by name)' for a hostname - never resolved, nothing is sent."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host.endswith(_LAN_SUFFIXES):
            return "lan (by name)"
        return "public (by name)"
    if address.is_private or address.is_loopback or address.is_link_local:
        return "private"
    return "public"


def _insecure(config: dict[str, Any]) -> bool:
    value = config.get("verify_ssl", True)
    return value is False or str(value).lower() == "false"


class RestCommandWriter:
    """Plans and writes rest_command entries in packages."""

    def __init__(self, hass: HomeAssistant, manager: RestCommandManager) -> None:
        self.hass = hass
        self.manager = manager

    async def _known_hosts(self, skip: str | None = None) -> set[str]:
        hosts = set()
        for _, command_id, config in await self.manager.all_rest_commands():
            url = config.get("url") if isinstance(config, dict) else None
            try:
                host = target_host(url)
            except RestCommandWriteError:
                continue
            if command_id != skip:
                hosts.add(host)
        return hosts

    async def plan_write(
        self,
        rest_command_id: str,
        config: dict[str, Any],
        *,
        package: str | None = None,
        allow_new_host: bool = False,
        allow_insecure_tls: bool = False,
    ) -> RestCommandPlan:
        """Every check first - all problems at once - then the content."""
        problems: list[str] = []
        locations = await self.manager.find_all_locations(rest_command_id)
        if len(locations) > 1:
            raise RestCommandWriteError(
                f"rest_command '{rest_command_id}' is defined in more than one "
                f"file: {[location.file_path for location in locations]}"
            )
        old: dict[str, Any] | None = None
        if locations:
            file_path = locations[0].file_path
            if file_path == DEFAULT_CONFIG_FILE:
                raise RestCommandWriteError(
                    f"'{rest_command_id}' is in configuration.yaml, which stays "
                    "read-only - move it into a package to change it here"
                )
            _, existing = await self.manager.get_rest_command(rest_command_id)
            old = dict(existing)
        elif package is not None:
            file_path = package_file(package)
        else:
            raise RestCommandWriteError(
                f"no rest_command '{rest_command_id}' yet - pass package to create it"
            )

        try:
            host = target_host(config.get("url"))
        except RestCommandWriteError as err:
            problems.append(str(err))
            host = None
        old_host = None
        if old is not None:
            try:
                old_host = target_host(old.get("url"))
            except RestCommandWriteError:
                old_host = None
        known = await self._known_hosts()
        new_host = host is not None and host not in known and host != old_host
        if host is not None and new_host and not allow_new_host:
            problems.append(
                f"'{host}' ({host_class(host)}) is a host no rest_command uses yet "
                "- pass allow_new_host to send requests there"
            )
        if credentials := mirror_secrets.literal_credentials(to_json_safe(config)):
            problems.append(
                f"literal credential(s) at {', '.join(credentials)} - use "
                "'!secret <name>' (secrets.yaml) instead"
            )
        if _insecure(config) and not (old is not None and _insecure(old)):
            if not allow_insecure_tls:
                problems.append(
                    "verify_ssl: false turns off certificate checks - pass "
                    "allow_insecure_tls if that's intended"
                )
        if problems:
            raise RestCommandWriteError("; ".join(problems))

        try:
            content = await self.manager.file_manager.read_file(file_path)
        except FileNotFoundError:
            raise RestCommandWriteError(
                f"package file '{file_path}' doesn't exist - create it first"
            ) from None
        prepared = tag_references(quote_ambiguous_scalars(dict(config)))
        after = await self.hass.async_add_executor_job(
            _build, content, rest_command_id, prepared
        )
        report = {
            "rest_command_id": rest_command_id,
            "file_path": file_path,
            "action": "replace" if old is not None else "create",
            "method": str(config.get("method", "get")).lower(),
            "timeout": config.get("timeout", _DEFAULT_TIMEOUT),
            "host": host,
            "host_class": host_class(host) if host else None,
            "new_host": new_host,
            "verify_ssl": not _insecure(config),
            "before": (
                mirror_secrets.mask_credentials(to_json_safe(old))
                if old is not None
                else None
            ),
            "after": mirror_secrets.mask_credentials(to_json_safe(dict(config))),
        }
        return RestCommandPlan(rest_command_id, file_path, content, after, report)

    async def plan_delete(self, rest_command_id: str) -> RestCommandPlan:
        location = await self.manager.find_rest_command(rest_command_id)
        if location.file_path == DEFAULT_CONFIG_FILE:
            raise RestCommandWriteError(
                f"'{rest_command_id}' is in configuration.yaml, which stays read-only"
            )
        _, old = await self.manager.get_rest_command(rest_command_id)
        content = await self.manager.file_manager.read_file(location.file_path)
        after = await self.hass.async_add_executor_job(
            _build, content, rest_command_id, None
        )
        return RestCommandPlan(
            rest_command_id,
            location.file_path,
            content,
            after,
            {
                "rest_command_id": rest_command_id,
                "file_path": location.file_path,
                "action": "delete",
                "before": mirror_secrets.mask_credentials(to_json_safe(dict(old))),
            },
        )

    async def write(self, plan: RestCommandPlan) -> str:
        """Write the plan's file and reload rest_command; say how it went."""
        await self.manager.file_manager.write_file(
            plan.file_path, plan.content_after, validate_before_write=True
        )
        if self.hass.services.has_service(REST_COMMAND_KEY, "reload"):
            await self.hass.services.async_call(
                REST_COMMAND_KEY, "reload", blocking=True
            )
            return "reloaded (rest_command.reload)"
        return "rest_command isn't loaded yet - restart Home Assistant to load it"


def _build(content: str, rest_command_id: str, config: dict[str, Any] | None) -> str:
    """Synchronous: `content` with the entry set (config) or removed (None)."""
    yaml = _new_yaml()
    yaml.preserve_quotes = True
    yaml.width = 4096
    document = _load_yaml(content)
    spliceable = True
    if not isinstance(document, dict):
        document, spliceable = CommentedMap(), False
    commands = document.get(REST_COMMAND_KEY)
    if not isinstance(commands, dict):
        commands, spliceable = CommentedMap(), False
        document[REST_COMMAND_KEY] = commands
    source = content if spliceable else None
    if config is None:
        last = len(commands) == 1
        container, key = (
            (document, REST_COMMAND_KEY) if last else (commands, rest_command_id)
        )

        def apply() -> None:
            del container[key]

        spliced = surgical_edit(source, container, key, "delete", yaml, apply)
    elif rest_command_id in commands:

        def apply() -> None:
            commands[rest_command_id] = merge_preserving_style(
                commands[rest_command_id], config
            )

        spliced = surgical_edit(
            source, commands, rest_command_id, "replace", yaml, apply
        )
    else:

        def apply() -> None:
            commands[rest_command_id] = config

        spliced = surgical_edit(
            source, commands, rest_command_id, "append", yaml, apply
        )
    buffer = StringIO()
    yaml.dump(document, buffer)
    return spliced_or_full(spliced, buffer.getvalue(), _load_yaml)
