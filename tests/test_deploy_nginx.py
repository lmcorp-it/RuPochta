"""Behavioral regression tests for the RuPochta nginx trust boundary.

The review host does not provide an nginx binary, Docker, or a WSL
distribution.  This test therefore parses the deployed nginx fragments into a
small semantic model of the directives involved in the trust decision:
``set_real_ip_from``, ``real_ip_header``, ``geo``, ``map``,
``limit_req_zone``, and ``proxy_set_header``.  The attack cases execute that
model; they do not assert that particular source strings merely exist.
"""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
import shlex
import unittest


ROOT = Path(__file__).resolve().parents[1]
LIMITS = ROOT / "deploy" / "nginx" / "conf.d" / "rupochta-limits.conf"
PROXY = ROOT / "deploy" / "nginx" / "snippets" / "rupochta-proxy.conf"
SITES = (
    ROOT / "deploy" / "nginx" / "rupochta-rf-bootstrap.conf",
    ROOT / "deploy" / "nginx" / "rupochta-rf.conf",
)


@dataclass(frozen=True)
class Directive:
    name: str
    args: tuple[str, ...]
    children: tuple["Directive", ...] = ()


def parse_nginx(path: Path) -> tuple[Directive, ...]:
    """Parse the nginx subset used by the production deployment fragments."""
    lexer = shlex.shlex(
        path.read_text(encoding="utf-8"),
        posix=True,
        punctuation_chars="{};",
    )
    lexer.commenters = "#"
    lexer.whitespace_split = True
    raw_tokens = list(lexer)
    tokens: list[str] = []
    for token in raw_tokens:
        if token and all(char in "{};" for char in token):
            tokens.extend(token)
        else:
            tokens.append(token)

    position = 0

    def parse_block(*, nested: bool) -> tuple[Directive, ...]:
        nonlocal position
        directives: list[Directive] = []
        head: list[str] = []
        while position < len(tokens):
            token = tokens[position]
            position += 1
            if token == ";":
                if not head:
                    raise AssertionError(f"empty directive in {path}")
                directives.append(Directive(head[0], tuple(head[1:])))
                head = []
                continue
            if token == "{":
                if not head:
                    raise AssertionError(f"anonymous block in {path}")
                children = parse_block(nested=True)
                directives.append(Directive(head[0], tuple(head[1:]), children))
                head = []
                continue
            if token == "}":
                if head:
                    raise AssertionError(f"unterminated directive in {path}")
                if not nested:
                    raise AssertionError(f"unexpected closing brace in {path}")
                return tuple(directives)
            head.append(token)

        if nested:
            raise AssertionError(f"unterminated block in {path}")
        if head:
            raise AssertionError(f"directive without semicolon in {path}")
        return tuple(directives)

    parsed = parse_block(nested=False)
    if position != len(tokens):
        raise AssertionError(f"unparsed tokens in {path}")
    return parsed


def walk(directives: tuple[Directive, ...]):
    for directive in directives:
        yield directive
        yield from walk(directive.children)


class NginxTrustModel:
    """Execute nginx's relevant address/header mapping semantics."""

    _VARIABLE = re.compile(r"\$[A-Za-z0-9_]+")

    def __init__(self) -> None:
        self.limits = parse_nginx(LIMITS)
        self.proxy = parse_nginx(PROXY)
        self.trusted_peers = tuple(
            ipaddress.ip_network(directive.args[0], strict=False)
            for directive in self.limits
            if directive.name == "set_real_ip_from"
        )
        headers = [
            directive.args[0]
            for directive in self.limits
            if directive.name == "real_ip_header"
        ]
        self.real_ip_header = headers[-1].lower() if headers else ""
        self.geos = tuple(
            directive for directive in self.limits if directive.name == "geo"
        )
        self.maps = tuple(
            directive for directive in self.limits if directive.name == "map"
        )
        zones = [
            directive
            for directive in self.limits
            if directive.name == "limit_req_zone"
        ]
        if len(zones) != 1:
            raise AssertionError("expected exactly one RuPochta limit_req_zone")
        self.rate_key_expression = zones[0].args[0]
        self.proxy_headers = {
            directive.args[0].lower(): directive.args[1]
            for directive in self.proxy
            if directive.name == "proxy_set_header"
        }

    @staticmethod
    def _header_variable(name: str) -> str:
        return "http_" + name.lower().replace("-", "_")

    @classmethod
    def _resolve(cls, expression: str, variables: dict[str, object]):
        if expression.startswith("$") and cls._VARIABLE.fullmatch(expression):
            return variables.get(expression[1:], "")
        return cls._VARIABLE.sub(
            lambda match: str(variables.get(match.group(0)[1:], "")),
            expression,
        )

    @staticmethod
    def _replacement_address(value: str) -> ipaddress._BaseAddress | None:
        # CF-Connecting-IP is a single address.  Matching nginx with
        # real_ip_recursive=off, a comma-separated value would use its last hop.
        candidate = value.rsplit(",", 1)[-1].strip()
        if not candidate:
            return None
        try:
            return ipaddress.ip_address(candidate)
        except ValueError:
            return None

    def _evaluate_geo(
        self,
        directive: Directive,
        variables: dict[str, object],
    ) -> tuple[str, str]:
        if len(directive.args) == 1:
            source_name, target_name = "remote_addr", directive.args[0][1:]
        else:
            source_name = directive.args[0][1:]
            target_name = directive.args[1][1:]
        source = ipaddress.ip_address(str(variables[source_name]))
        default = ""
        best_match: tuple[int, str] | None = None
        for entry in directive.children:
            value = entry.args[0]
            if entry.name == "default":
                default = value
                continue
            network = ipaddress.ip_network(entry.name, strict=False)
            if source in network and (
                best_match is None or network.prefixlen > best_match[0]
            ):
                best_match = (network.prefixlen, value)
        return target_name, best_match[1] if best_match else default

    def _evaluate_map(
        self,
        directive: Directive,
        variables: dict[str, object],
    ) -> tuple[str, object]:
        source = str(self._resolve(directive.args[0], variables))
        target_name = directive.args[1][1:]
        default = ""
        selected: str | None = None
        for entry in directive.children:
            value = " ".join(entry.args)
            if entry.name == "default":
                default = value
            elif entry.name.casefold() == source.casefold():
                selected = value
                break
        return target_name, self._resolve(selected if selected is not None else default, variables)

    def forward(
        self,
        *,
        peer: str,
        scheme: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        original_peer = ipaddress.ip_address(peer)
        normalized_headers = {
            name.lower(): value for name, value in (headers or {}).items()
        }
        effective_client = original_peer
        if any(original_peer in network for network in self.trusted_peers):
            replacement = self._replacement_address(
                normalized_headers.get(self.real_ip_header, "")
            )
            if replacement is not None:
                effective_client = replacement

        variables: dict[str, object] = {
            "remote_addr": str(effective_client),
            "realip_remote_addr": str(original_peer),
            "binary_remote_addr": effective_client.packed,
            "scheme": scheme,
        }
        variables.update(
            {
                self._header_variable(name): value
                for name, value in normalized_headers.items()
            }
        )
        for directive in self.geos:
            name, value = self._evaluate_geo(directive, variables)
            variables[name] = value
        for directive in self.maps:
            name, value = self._evaluate_map(directive, variables)
            variables[name] = value

        return {
            "client_ip": variables["remote_addr"],
            "rate_key": self._resolve(self.rate_key_expression, variables),
            "x_real_ip": self._resolve(
                self.proxy_headers["x-real-ip"], variables
            ),
            "x_forwarded_proto": self._resolve(
                self.proxy_headers["x-forwarded-proto"], variables
            ),
        }


class NginxTrustBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.nginx = NginxTrustModel()

    def test_only_loopback_peers_may_supply_the_real_ip_header(self):
        self.assertEqual(
            set(self.nginx.trusted_peers),
            {
                ipaddress.ip_network("127.0.0.1/32"),
                ipaddress.ip_network("::1/128"),
            },
        )

    def test_ipv4_cloudflared_request_uses_visitor_for_headers_and_rate_limit(self):
        result = self.nginx.forward(
            peer="127.0.0.1",
            scheme="http",
            headers={
                "CF-Connecting-IP": "203.0.113.41",
                "X-Forwarded-Proto": "https",
            },
        )
        self.assertEqual(result["client_ip"], "203.0.113.41")
        self.assertEqual(result["x_real_ip"], "203.0.113.41")
        self.assertEqual(result["rate_key"], ipaddress.ip_address("203.0.113.41").packed)
        self.assertEqual(result["x_forwarded_proto"], "https")

    def test_tunnel_visitors_do_not_share_one_rate_limit_bucket(self):
        keys = {
            self.nginx.forward(
                peer="127.0.0.1",
                scheme="http",
                headers={"CF-Connecting-IP": visitor},
            )["rate_key"]
            for visitor in ("203.0.113.41", "203.0.113.42")
        }
        self.assertEqual(len(keys), 2)

    def test_ipv6_loopback_tunnel_and_visitor_are_supported(self):
        result = self.nginx.forward(
            peer="::1",
            scheme="http",
            headers={
                "CF-Connecting-IP": "2001:db8::41",
                "X-Forwarded-Proto": "https",
            },
        )
        self.assertEqual(result["client_ip"], "2001:db8::41")
        self.assertEqual(result["x_real_ip"], "2001:db8::41")
        self.assertEqual(result["rate_key"], ipaddress.ip_address("2001:db8::41").packed)
        self.assertEqual(result["x_forwarded_proto"], "https")

    def test_direct_client_cannot_spoof_ip_or_https(self):
        result = self.nginx.forward(
            peer="198.51.100.23",
            scheme="http",
            headers={
                "CF-Connecting-IP": "10.0.0.1",
                "X-Forwarded-Proto": "https",
            },
        )
        self.assertEqual(result["client_ip"], "198.51.100.23")
        self.assertEqual(result["x_real_ip"], "198.51.100.23")
        self.assertEqual(result["rate_key"], ipaddress.ip_address("198.51.100.23").packed)
        self.assertEqual(result["x_forwarded_proto"], "http")

    def test_loopback_operator_without_forwarding_headers_still_works(self):
        for peer in ("127.0.0.1", "::1"):
            with self.subTest(peer=peer):
                result = self.nginx.forward(peer=peer, scheme="http")
                self.assertEqual(result["client_ip"], peer)
                self.assertEqual(result["x_real_ip"], peer)
                self.assertEqual(result["x_forwarded_proto"], "http")

    def test_bootstrap_and_production_proxy_locations_share_the_snippet(self):
        expected_include = "/etc/nginx/snippets/rupochta-proxy.conf"
        for site in SITES:
            with self.subTest(site=site.name):
                locations = [
                    directive
                    for directive in walk(parse_nginx(site))
                    if directive.name == "location"
                    and any(child.name == "proxy_pass" for child in directive.children)
                ]
                self.assertTrue(locations, f"no proxy locations in {site}")
                for location in locations:
                    includes = {
                        child.args[0]
                        for child in location.children
                        if child.name == "include"
                    }
                    self.assertIn(expected_include, includes)


if __name__ == "__main__":
    unittest.main()
