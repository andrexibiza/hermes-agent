"""Parent-side egress guard plumbing for ``browser_exec`` (Region B).

Generates and verifies the stdlib-only child interposer pair, injects the
per-spawn policy/nonce, parses tamper-evident markers, and pins a Hermes
filtering forward proxy for proxy-honoring descendants.

The policy snapshot is monotonic: an execution-local request can tighten the
configured private-network policy, never widen a configured deny. The proxy
uses the same resolve-once verdict object as the URL policy and dials only the
returned literal IPs. HTTP redirects are followed explicitly and every hop is
re-authorized; CONNECT tunnels likewise dial the checked literal instead of
re-resolving the hostname.

Threat boundary: the Python interposer covers the CLI process and proxy env
pinning covers descendants that honor those variables. Env-stripped/native
children remain outside this PR's Python-level boundary; OS firewall,
seccomp, namespaces, and sandbox enforcement are separate layers.
"""

import hashlib
import http.client
import ipaddress
import json
import logging
import os
import secrets
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

_GUARD_PREFIX = "__HERMES_EGRESS_GUARD__:"
_CHILD_FILES = ("sitecustomize.py", "browser_exec_egress_guard.py")


def _egress_guard_dir() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "cache" / "browser-use" / "egress-guard"


def _egress_guard_source() -> dict:
    pkg = Path(__file__).parent / "browser_use_guard_bootstrap"
    return {name: (pkg / name).read_text(encoding="utf-8") for name in _CHILD_FILES}


def _file_sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _verify_or_regenerate(guard_dir: Path) -> None:
    source = _egress_guard_source()
    guard_dir.mkdir(parents=True, exist_ok=True)
    for name, content in source.items():
        path = guard_dir / name
        encoded = content.encode("utf-8")
        try:
            if _file_sha256(path.read_bytes()) == _file_sha256(encoded):
                continue
        except OSError:
            pass
        try:
            path.write_text(content, encoding="utf-8")
            logger.debug("egress guard: regenerated %s", path)
        except OSError as exc:  # pragma: no cover - unwritable cache
            logger.warning("egress guard: cannot write %s: %s", path, exc)


def _policy_snapshot(allow_private: Optional[bool] = None, nonce: str = "") -> dict:
    """Render a frozen, monotonic child policy snapshot.

    ``allow_private`` is a tightening input, not an override. ``True`` cannot
    turn a configured deny into an allow; ``False`` can tighten a configured
    allow for one execution.
    """
    from tools.url_safety import (
        _ALWAYS_BLOCKED_IPS,
        _ALWAYS_BLOCKED_NETWORKS,
        _BLOCKED_HOSTNAMES,
        _CGNAT_NETWORK,
        _global_allow_private_urls,
    )

    configured_allow = bool(_global_allow_private_urls())
    effective_allow = (
        configured_allow
        if allow_private is None
        else configured_allow and bool(allow_private)
    )
    return {
        "nonce": nonce,
        "allow_private": effective_allow,
        "blocked_hostnames": sorted(str(h) for h in _BLOCKED_HOSTNAMES),
        "always_blocked_ips": sorted(str(ip) for ip in _ALWAYS_BLOCKED_IPS),
        "always_blocked_networks": sorted(str(net) for net in _ALWAYS_BLOCKED_NETWORKS),
        "cgnat_network": str(_CGNAT_NETWORK),
        "allow_hosts": [],
    }


def _egress_guard_enabled() -> bool:
    try:
        from tools.browser_use_cli import _read_browser_cfg

        value = _read_browser_cfg().get("exec_egress_guard")
        if value is False or str(value or "").strip().lower() == "off":
            return False
    except Exception:
        pass
    return os.environ.get("HERMES_BROWSER_EXEC_EGRESS_GUARD", "") != "0"


def _install_egress_guard(env: dict) -> bool:
    if not _egress_guard_enabled():
        logger.warning(
            "browser_exec egress guard disabled by config "
            "(browser.exec_egress_guard: off) â€” CLI subprocess egress is "
            "not interposed."
        )
        return False

    guard_dir = _egress_guard_dir()
    _verify_or_regenerate(guard_dir)

    nonce = secrets.token_hex(8)
    env["HERMES_BROWSER_EXEC_EGRESS_GUARD"] = "1"
    env["HERMES_BROWSER_EXEC_EGRESS_GUARD_NONCE"] = nonce

    policy = _policy_snapshot(nonce=nonce)
    try:
        from tools.browser_use_cli import _read_browser_cfg

        allow_cfg = _read_browser_cfg().get("exec_egress_allow") or ""
        if isinstance(allow_cfg, (list, tuple)):
            allow_cfg = ",".join(str(value) for value in allow_cfg)
        if allow_cfg:
            env["HERMES_BROWSER_EXEC_EGRESS_ALLOW"] = str(allow_cfg)
            policy["allow_hosts"] = [
                host.strip()
                for host in str(allow_cfg).split(",")
                if host.strip()
            ]
    except Exception:
        pass
    env["HERMES_BROWSER_EXEC_EGRESS_POLICY"] = json.dumps(policy, sort_keys=True)

    prepend = str(guard_dir)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = prepend + (os.pathsep + existing if existing else "")
    _pin_egress_proxy(env)
    return True


_MARKER_RE = None


def _marker_re():
    import re

    global _MARKER_RE
    if _MARKER_RE is None:
        _MARKER_RE = re.compile(
            r"^" + re.escape(_GUARD_PREFIX) + r"(installed|block|tamper|disabled):(.*)$"
        )
    return _MARKER_RE


def _parse_guard_markers(stderr: str, nonce: str) -> Optional[str]:
    installed_ok = False
    for line in (stderr or "").splitlines():
        match = _marker_re().match(line.strip())
        if not match:
            continue
        kind, payload = match.group(1), match.group(2).strip()
        if kind == "installed":
            installed_ok = payload == nonce
        elif kind == "block":
            return (
                "Blocked: browser_exec attempted a direct connection to a "
                f"private or internal address ({payload}); output withheld."
            )
        elif kind == "tamper":
            return (
                "Blocked: the browser_exec egress guard detected binding "
                f"tamper ({payload}); output withheld."
            )
        elif kind == "disabled":
            return (
                "Blocked: the browser_exec egress guard disabled itself "
                f"({payload}); output withheld."
            )
    if not installed_ok:
        return (
            "Blocked: the browser_exec egress guard did not report a "
            "verified install (missing or nonce-mismatched :installed: "
            "marker); output withheld."
        )
    return None


def _strip_guard_markers(stderr: str) -> str:
    if _GUARD_PREFIX not in (stderr or ""):
        return stderr
    return "\n".join(
        line for line in stderr.splitlines() if _GUARD_PREFIX not in line
    )


# â”€â”€ Filtering proxy: resolve once, authorize object, dial same object â”€â”€â”€â”€â”€â”€
_PROXY_LOCK = threading.Lock()
_PROXY_INSTANCE = None
_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))
_HOP_BY_HOP_HEADERS = frozenset(
    (
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    )
)


class _EgressPolicyBlocked(RuntimeError):
    pass


class _EgressUpstreamError(RuntimeError):
    pass


def _resolve_checked_target(url: str):
    """Return ``(parsed_url, approved_ip_tuple)`` from one policy verdict."""
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise _EgressPolicyBlocked(f"blocked:parse ({exc})") from exc
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise _EgressPolicyBlocked("blocked:unsupported-or-missing-authority")

    from tools.url_safety import resolve_and_check_url

    verdict = resolve_and_check_url(url)
    if not verdict.ok or not verdict.resolved_ips:
        raise _EgressPolicyBlocked(verdict.reason or "blocked:policy")
    return parsed, tuple(str(ip) for ip in verdict.resolved_ips)


def _dial_checked_ips(approved_ips: Iterable[str], port: int, timeout: float):
    """Dial one of the already-authorized literals without hostname lookup."""
    errors = []
    for literal in approved_ips:
        try:
            parsed = ipaddress.ip_address(str(literal).split("%", 1)[0])
        except ValueError as exc:
            errors.append(exc)
            continue
        family = socket.AF_INET6 if parsed.version == 6 else socket.AF_INET
        address = (str(parsed), port, 0, 0) if parsed.version == 6 else (str(parsed), port)
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout)
            sock.connect(address)
            return sock
        except OSError as exc:
            errors.append(exc)
            try:
                sock.close()
            except OSError:
                pass
    detail = errors[-1] if errors else "no approved address"
    raise OSError(f"all approved upstream addresses failed: {detail}")


def _authority_header(parsed) -> str:
    host = parsed.hostname or ""
    rendered = f"[{host}]" if ":" in host and not host.startswith("[") else host
    default = 443 if parsed.scheme.lower() == "https" else 80
    try:
        port = parsed.port
    except ValueError as exc:
        raise _EgressPolicyBlocked(f"blocked:parse ({exc})") from exc
    return rendered if port in (None, default) else f"{rendered}:{port}"


def _request_headers(headers, parsed, body) -> dict:
    out = {}
    for key, value in dict(headers or {}).items():
        lower = str(key).lower()
        if lower in _HOP_BY_HOP_HEADERS or lower in ("host", "content-length"):
            continue
        out[str(key)] = str(value)
    out["Host"] = _authority_header(parsed)
    if body is not None:
        out["Content-Length"] = str(len(body))
    return out


def _open_checked_request(
    target: str,
    method: str,
    headers,
    body,
    *,
    timeout: float = 30.0,
):
    """Open one HTTP(S) hop over an exact policy-approved socket."""
    parsed, approved_ips = _resolve_checked_target(target)
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise _EgressPolicyBlocked(f"blocked:parse ({exc})") from exc

    raw = _dial_checked_ips(approved_ips, port, timeout)
    try:
        if parsed.scheme.lower() == "https":
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=parsed.hostname)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
        connection.sock = raw
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        connection.request(
            method,
            path,
            body=body,
            headers=_request_headers(headers, parsed, body),
        )
        return connection, connection.getresponse()
    except Exception:
        try:
            raw.close()
        except OSError:
            pass
        raise


def _request_with_checked_redirects(
    method: str,
    target: str,
    headers,
    body,
    *,
    timeout: float = 30.0,
    max_hops: int = 6,
):
    """Run a request chain; every redirect hop gets a fresh pinned verdict."""
    current_method = str(method or "GET").upper()
    current_target = target
    current_body = body
    current_headers = dict(headers or {})

    for _hop in range(max_hops):
        connection = None
        try:
            connection, response = _open_checked_request(
                current_target,
                current_method,
                current_headers,
                current_body,
                timeout=timeout,
            )
            status = int(response.status)
            reason = str(response.reason or "")
            response_headers = list(response.getheaders())
            if status in _REDIRECT_STATUSES:
                location = response.getheader("Location")
                response.read()
                if not location:
                    raise _EgressUpstreamError("redirect without Location")
                previous = urlsplit(current_target)
                current_target = urljoin(current_target, location)
                following = urlsplit(current_target)
                if (
                    previous.scheme.lower(),
                    previous.hostname,
                    previous.port,
                ) != (
                    following.scheme.lower(),
                    following.hostname,
                    following.port,
                ):
                    current_headers.pop("Authorization", None)
                    current_headers.pop("Cookie", None)
                if status == 303 or (
                    status in (301, 302) and current_method not in ("GET", "HEAD")
                ):
                    current_method = "GET"
                    current_body = None
                    current_headers.pop("Content-Type", None)
                    current_headers.pop("Content-Length", None)
                continue
            payload = response.read()
            return status, reason, response_headers, payload
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
    raise _EgressUpstreamError("too many redirects")


class _EgressFilterProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # pragma: no cover - noisy by default
        pass

    def _blocked(self, url: str) -> bool:
        try:
            _resolve_checked_target(url)
            return False
        except Exception:
            return True

    def _reply(self, code: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._forward_absolute()

    def do_POST(self):  # noqa: N802
        self._forward_absolute()

    def doWÔU§6VÆb“¢2æ÷¢ãƒ ¢6VÆbåöf÷'v&Eö'6öÇWFR‚ ¢FVbFõôDTÄUDR‡6VÆb“¢2æ÷¢ãƒ ¢6VÆbåöf÷'v&Eö'6öÇWFR‚ ¢FVbFõô„TB‡6VÆb“¢2æ÷¢ãƒ ¢6VÆbåöf÷'v&Eö'6öÇWFR‚ ¢FVböf÷'v&Eö'6öÇWFR‡6VÆb’ÓâæöæS ¢F&vWBÒ6VÆbçF€¢–bæ÷BF&vWBæÆ÷vW"‚’ç7F'G7v—F‚‚‚&‡GG¢òò"Â&‡GG3¢òò"’“ ¢6VÆbå÷&WÇ’ƒCÂ"&öæÇ’'6öÇWFRÖf÷&Ò…EE&WVW7G2&R&÷†–VB"¢&WGW&à¢G'“ ¢ÆVæwF‚Ò–çB‡6VÆbæ†VFW'2ævWB‚$6öçFVçBÔÆVæwF‚"’÷"¢W†6WBfÇVTW'&÷# ¢6VÆbå÷&WÇ’ƒCÂ"&–çfÆ–B6öçFVçBÔÆVæwF‚"¢&WGW&à¢&öG’Ò6VÆbç&f–ÆRç&VB†ÆVæwF‚’–bÆVæwF‚VÇ6RæöæP¢G'“ ¢7FGW2Â&V6öâÂ&W7öç6Uö†VFW'2Â–ÆöBÒ÷&WVW7E÷v—F…ö6†V6¶VE÷&VF—&V7G2€¢6VÆbæ6öÖÖæBÀ¢F&vWBÀ¢6VÆbæ†VFW'2À¢&öG’À¢¢W†6WBôVw&W75öÆ–7”&Æö6¶VC ¢6VÆbå÷&WÇ’ƒC2Â"$&Æö6¶VB'’†W&ÖW2'&÷w6W%öW†V2Vw&W72wV&B"¢&WGW&à¢W†6WBW†6WF–öã ¢6VÆbå÷&WÇ’ƒS"Â"'W7G&VÒf–ÇW&R"¢&WGW&à ¢6VÆbç6VæE÷&W7öç6R‡7FGW2Â&V6öâ¢f÷"¶W’ÂfÇVR–â&W7öç6Uö†VFW'3 ¢Æ÷vW"Ò¶W’æÆ÷vW"‚¢–bÆ÷vW"–âô„õô%•ô„õô„TDU%2÷"Æ÷vW"ÓÒ&6öçFVçBÖÆVæwF‚# ¢6öçF–çVP¢6VÆbç6VæEö†VFW"†¶W’ÂfÇVR¢6VÆbç6VæEö†VFW"‚$6öçFVçBÔÆVæwF‚"Â7G"†ÆVâ‡–ÆöB’’¢6VÆbæVæEö†VFW'2‚¢–b6VÆbæ6öÖÖæBÒ$„TB# ¢6VÆbçvf–ÆRçw&—FR‡–ÆöB ¢FVbFõô4ôääT5B‡6VÆb“¢2æ÷¢ãƒ ¢G'“ ¢'6VEöWF†÷&—G’ÒW&Ç7Æ—B‚"òò"²6VÆbçF‚¢†÷7BÒ'6VEöWF†÷&—G’æ†÷7FæÖP¢÷'BÒ'6VEöWF†÷&—G’ç÷'@¢W†6WBfÇVTW'&÷# ¢†÷7BÒæöæP¢÷'BÒæöæP¢–bæ÷B†÷7B÷"÷'B—2æöæS ¢6VÆbå÷&WÇ’ƒCÂ"&ÖÆf÷&ÖVB4ôääT5BF&vWB"¢&WGW&à ¢&VæFW&VEö†÷7BÒb%·¶†÷7GÕÒ"–b#¢"–â†÷7BVÇ6R†÷7@¢G'“ ¢÷'6VBÂ&÷fVEö—2Ò÷&W6öÇfUö6†V6¶VE÷F&vWB€¢b&‡GG3¢ò÷·&VæFW&VEö†÷7GÓ§·÷'GÒò ¢¢W7G&VÒÒöF–Åö6†V6¶VEö—2†&÷fVEö—2Â÷'BÂ3ã¢W†6WBôVw&W75öÆ–7”&Æö6¶VC ¢6VÆbå÷&WÇ’ƒC2Â"$&Æö6¶VB'’†W&ÖW2'&÷w6W%öW†V2Vw&W72wV&B"¢&WGW&à¢W†6WBõ4W'&÷# ¢6VÆbå÷&WÇ’ƒS"Â"'W7G&VÒ6öææV7Bf–ÆVB"¢&WGW&à ¢6VÆbç6VæE÷&W7öç6Rƒ#Â$6öææV7F–öâW7F&Æ—6†VB"¢6VÆbç6VæEö†VFW"‚$6öçFVçBÔÆVæwF‚"Â#"¢6VÆbæVæEö†VFW'2‚¢G'“ ¢÷V×‡6VÆbæ6öææV7F–öâÂW7G&VÒ¢W†6WBW†6WF–öã ¢70¢f–æÆÇ“ ¢G'“ ¢W7G&VÒæ6Æ÷6R‚¢W†6WBõ4W'&÷# ¢70  ¦FVbö—5öÆ—FW&Â††÷7C¢7G"’Óâ&ööÃ ¢G'“ ¢—FG&W72æ—öFG&W72††÷7B¢&WGW&âG'VP¢W†6WBfÇVTW'&÷# ¢&WGW&âfÇ6P  ¦FVbö—ö&Æö6¶VEöf÷%ö6öææV7B†—÷7G#¢7G"’Óâ&ööÃ ¢g&öÒFööÇ2çW&Å÷6fWG’–×÷'B—ö—5ö&Æö6¶V@ ¢G'“ ¢&Æö6¶VBÂòÒ—ö—5ö&Æö6¶VB†—÷7G"¢&WGW&â&Æö6¶V@¢W†6WBW†6WF–öã ¢&WGW&âG'VP  ¦FVb÷V×†6Æ–VçC¢6ö6¶WBç6ö6¶WBÂW7G&VÓ¢6ö6¶WBç6ö6¶WB’ÓâæöæS ¢–×÷'B6VÆV7@ ¢6Æ–VçBç6WF&Æö6¶–ær„fÇ6R¢W7G&VÒç6WF&Æö6¶–ær„fÇ6R¢G'“ ¢v†–ÆRG'VS ¢&VF&ÆRÂòÂòÒ6VÆV7Bç6VÆV7B…¶6Æ–VçBÂW7G&VÕÒÂµÒÂµÒÂã¢–bæ÷B&VF&ÆS ¢6öçF–çVP¢f÷"6ö6²–â&VF&ÆS ¢G'“ ¢FFÒ6ö6²ç&V7bƒcSS3b¢W†6WBõ4W'&÷# ¢&WGW&à¢–bæ÷BFF ¢&WGW&à¢VW"ÒW7G&VÒ–b6ö6²—26Æ–VçBVÇ6R6Æ–Vç@¢G'“ ¢VW"ç6VæFÆÂ†FF¢W†6WBõ4W'&÷# ¢&WGW&à¢f–æÆÇ“ ¢6Æ–VçBç6WF&Æö6¶–ær…G'VR  ¦6Æ72ôVw&W74f–ÇFW%&÷‡“ ¢FVbõö–æ—Eõò‡6VÆb’ÓâæöæS ¢6VÆbå÷6W'fW#¢÷F–öæÅµF‡&VF–æt…EE6W'fW%ÒÒæöæP¢6VÆbå÷F‡&VC¢÷F–öæÅ·F‡&VF–æråF‡&VEÒÒæöæP ¢FVb7F'B‡6VÆb’Óâ÷F–öæÅ¶–çEÓ ¢–b6VÆbå÷6W'fW"—2æ÷BæöæS ¢&WGW&â6VÆbå÷6W'fW"ç6W'fW%öFG&W75³Ð¢G'“ ¢6VÆbå÷6W'fW"ÒF‡&VF–æt…EE6W'fW"€¢‚##rããã"Â’ÂôVw&W74f–ÇFW%&÷‡”†æFÆW ¢¢W†6WBõ4W'&÷"2W†3 ¢ÆövvW"çv&æ–ær‚&Vw&W72&÷‡’6÷VÆBæ÷B7F'C¢W2"ÂW†2¢&WGW&âæöæP¢6VÆbå÷F‡&VBÒF‡&VF–æråF‡&VB€¢F&vWC×6VÆbå÷6W'fW"ç6W'fUöf÷&WfW"À¢æÖSÒ&†W&ÖW2ÖVw&W72Öf–ÇFW"×&÷‡’"À¢FVÖöãÕG'VRÀ¢¢6VÆbå÷F‡&VBç7F'B‚¢&WGW&â6VÆbå÷6W'fW"ç6W'fW%öFG&W75³Ð  ¦FVb÷–åöVw&W75÷&÷‡’†Vçc¢F–7B’ÓâæöæS ¢vÆö&Âõ$õ…•ô”å5Dä4P¢v—F‚õ$õ…•ôÄô4³ ¢–bõ$õ…•ô”å5Dä4R—2æöæS ¢÷'BÒôVw&W74f–ÇFW%&÷‡’‚’ç7F'B‚¢–b÷'B—2æöæS ¢&—6R'VçF–ÖTW'&÷"€¢&'&÷w6W%öW†V2Vw&W72wV&B6÷VÆBæ÷B7F'B—G2f–ÇFW&–ær ¢'&÷‡“²&÷‡’Ö†öæ÷&–ær7væVBFööÇ2v÷VÆB&RVæf–ÇFW&VB ¢"†f–ÂÖ6Æ÷6VB’ ¢¢õ$õ…•ô”å5Dä4RÒ‚##rããã"Â÷'B¢†÷7BÂ÷'BÒõ$õ…•ô”å5Dä4P¢&÷‡•÷W&ÂÒb&‡GG¢ò÷¶†÷7GÓ§·÷'GÒ ¢f÷"f"–â€¢$…EEõ$õ…’"À¢&‡GG÷&÷‡’"À¢$…EE5õ$õ…’"À¢&‡GG5÷&÷‡’"À¢$ÄÅõ$õ…’"À¢&ÆÅ÷&÷‡’"À¢“ ¢Vçe·f%ÒÒ&÷‡•÷W&À¢Vçe²$äõõ$õ…’%ÒÒ##rãããÆÆö6Æ†÷7BÃ££ ¢Vçbç6WFFVfVÇB‚&æõ÷&÷‡’"Â##rãããÆÆö6Æ†÷7BÃ££"