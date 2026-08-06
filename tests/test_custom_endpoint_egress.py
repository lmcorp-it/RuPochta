"""Regression tests for the SEC-003 egress policy on custom mailbox endpoints.

`_reject_ssrf_targets` is the only thing standing between a user-supplied
("custom"/BYOM) IMAP or SMTP host and an outbound connection from the mail
server, so it is exercised here against the real implementation rather than a
stub. Name resolution is faked so the policy — not the CI network — decides
the outcome.

This guard previously shipped with no coverage at all, which let a rename
break it silently.
"""

import ipaddress
import os
import socket
import types
import unittest
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")

ALLOWLIST_ENV = "MAIL_CUSTOM_ENDPOINT_ALLOWLIST"


def _extract_function(name: str) -> str:
    start = SOURCE.index(f"def {name}(")
    ends = [
        SOURCE.find(marker, start + 1)
        for marker in ("\n\ndef ", "\n\nclass ", "\n\n@app", "\n\n# ")
    ]
    end = min(position for position in ends if position != -1)
    return SOURCE[start:end]


def _extract_allowed_ports() -> set:
    start = SOURCE.index("_CUSTOM_ENDPOINT_ALLOWED_PORTS = ")
    end = SOURCE.index("\n", start)
    namespace: Dict[str, Any] = {}
    exec(SOURCE[start:end], namespace)  # noqa: S102 - trusted own source
    return namespace["_CUSTOM_ENDPOINT_ALLOWED_PORTS"]


def _load_guard(resolved: Dict[str, List[str]]):
    """Load the egress guard with a fake resolver.

    `resolved` maps a hostname to the list of IPs it should resolve to. A host
    that is absent raises gaierror, standing in for an NXDOMAIN.
    """

    def fake_getaddrinfo(host, port, **_kwargs):
        if host not in resolved:
            raise socket.gaierror(f"fake NXDOMAIN for {host}")
        return [(None, None, None, None, (ip, port or 0)) for ip in resolved[host]]

    namespace: Dict[str, Any] = {
        "os": os,
        "ipaddress": ipaddress,
        "socket": types.SimpleNamespace(
            getaddrinfo=fake_getaddrinfo,
            gaierror=socket.gaierror,
            IPPROTO_TCP=socket.IPPROTO_TCP,
        ),
        "List": List,
        "_CUSTOM_ENDPOINT_ALLOWED_PORTS": _extract_allowed_ports(),
    }
    for name in (
        "_custom_endpoint_admin_allowlist",
        "_host_is_admin_allowlisted",
        "_reject_ssrf_targets",
    ):
        exec(_extract_function(name), namespace)  # noqa: S102 - trusted own source
    return types.SimpleNamespace(**namespace)


class CustomEndpointEgressTests(unittest.TestCase):
    def setUp(self):
        self._saved_allowlist = os.environ.get(ALLOWLIST_ENV)
        os.environ.pop(ALLOWLIST_ENV, None)

    def tearDown(self):
        if self._saved_allowlist is None:
            os.environ.pop(ALLOWLIST_ENV, None)
        else:
            os.environ[ALLOWLIST_ENV] = self._saved_allowlist

    def test_the_documented_mail_ports_are_the_only_ones_allowed(self):
        self.assertEqual(_extract_allowed_ports(), {993, 465, 587})

    def test_a_public_host_on_a_mail_port_is_accepted(self):
        guard = _load_guard({"imap.example.com": ["93.184.216.34"]})
        for port in (993, 465, 587):
            guard._reject_ssrf_targets("imap.example.com", port)

    def test_a_non_mail_port_is_refused_before_any_lookup(self):
        # Deliberately resolvable and public: the port alone must refuse it.
        guard = _load_guard({"imap.example.com": ["93.184.216.34"]})
        for port in (22, 80, 2375, 5432, 6379, 1993):
            with self.assertRaisesRegex(ValueError, "not permitted"):
                guard._reject_ssrf_targets("imap.example.com", port)

    def test_internal_addresses_are_refused_on_an_allowed_port(self):
        cases = {
            "loopback.test": "127.0.0.1",
            "private-a.test": "10.0.0.5",
            "private-b.test": "172.16.4.9",
            "private-c.test": "192.168.1.20",
            "linklocal.test": "169.254.169.254",  # cloud metadata
            "multicast.test": "224.0.0.1",
            "unspecified.test": "0.0.0.0",
            "v6-loopback.test": "::1",
            "v6-private.test": "fd00::1",
        }
        for host, ip in cases.items():
            with self.subTest(host=host, ip=ip):
                guard = _load_guard({host: [ip]})
                with self.assertRaisesRegex(ValueError, "private/loopback/reserved"):
                    guard._reject_ssrf_targets(host, 993)

    def test_a_host_is_refused_if_any_resolved_address_is_internal(self):
        # DNS rebinding: one public answer must not launder a private one.
        guard = _load_guard({"split.test": ["93.184.216.34", "127.0.0.1"]})
        with self.assertRaisesRegex(ValueError, "private/loopback/reserved"):
            guard._reject_ssrf_targets("split.test", 993)

    def test_the_ipv6_metadata_alias_is_refused(self):
        guard = _load_guard({"meta.test": ["fd00:ec2::254"]})
        # Caught either as private or by the explicit metadata check; both are
        # a refusal, which is what matters.
        with self.assertRaises(ValueError):
            guard._reject_ssrf_targets("meta.test", 993)

    def test_an_empty_host_is_refused(self):
        guard = _load_guard({})
        for host in ("", "   ", None):
            with self.assertRaisesRegex(ValueError, "host is required"):
                guard._reject_ssrf_targets(host, 993)

    def test_an_unresolvable_host_is_refused(self):
        guard = _load_guard({})
        with self.assertRaisesRegex(ValueError, "could not resolve"):
            guard._reject_ssrf_targets("nope.invalid", 993)

    def test_an_admin_allowlisted_host_may_use_a_private_address(self):
        os.environ[ALLOWLIST_ENV] = "imap.corp.local"
        guard = _load_guard({"imap.corp.local": ["10.1.2.3"]})
        guard._reject_ssrf_targets("imap.corp.local", 993)

    def test_an_admin_allowlisted_host_may_use_a_non_standard_port(self):
        os.environ[ALLOWLIST_ENV] = "imap.corp.local"
        guard = _load_guard({"imap.corp.local": ["10.1.2.3"]})
        guard._reject_ssrf_targets("imap.corp.local", 1993)

    def test_the_allowlist_accepts_a_cidr_for_a_literal_address(self):
        os.environ[ALLOWLIST_ENV] = "10.1.0.0/16"
        guard = _load_guard({"10.1.2.3": ["10.1.2.3"]})
        guard._reject_ssrf_targets("10.1.2.3", 993)
        # A literal outside the CIDR gets no exemption.
        other = _load_guard({"10.9.2.3": ["10.9.2.3"]})
        with self.assertRaisesRegex(ValueError, "private/loopback/reserved"):
            other._reject_ssrf_targets("10.9.2.3", 993)

    def test_an_empty_allowlist_exempts_nothing(self):
        for raw in ("", "   ", ",", " , , "):
            with self.subTest(allowlist=repr(raw)):
                os.environ[ALLOWLIST_ENV] = raw
                guard = _load_guard({"imap.corp.local": ["10.1.2.3"]})
                with self.assertRaisesRegex(ValueError, "private/loopback/reserved"):
                    guard._reject_ssrf_targets("imap.corp.local", 993)


if __name__ == "__main__":
    unittest.main()
