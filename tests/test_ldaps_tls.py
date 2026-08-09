"""Пароль пользователя уходит на контроллер домена по LDAPS, поэтому сертификат
обязан проверяться. ldap3 по умолчанию создаёт Tls(validate=CERT_NONE) и
принимает любой сертификат — тесты ниже поднимают настоящий TLS-сервер и
проверяют, что подделка не проходит (issue #39)."""

from pathlib import Path
import ast
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")

try:
    from ldap3.core.tls import check_hostname as ldap3_check_hostname
    from ldap3.core.exceptions import LDAPCertificateError
    LDAP3_PRESENT = True
except Exception:  # pragma: no cover
    LDAP3_PRESENT = False
    LDAPCertificateError = Exception


def _openssl_present() -> bool:
    return bool(shutil.which("openssl"))


def _supports_not_after() -> bool:
    """`-not_after` появился только в OpenSSL 3.5. На более старых сборках
    просроченный сертификат выписывается через `openssl ca` (см. _expired_cert)."""
    if not _openssl_present():
        return False
    done = subprocess.run(["openssl", "req", "-help"], capture_output=True, text=True)
    # На части сборок справка уходит в stdout, на части — в stderr.
    return "-not_after" in (done.stdout + done.stderr)


def _run(*args: str) -> None:
    subprocess.run(args, check=True, capture_output=True)


class LdapsCertificateValidationTests(unittest.TestCase):
    """Сценарии соответствуют способам подмены контроллера домена: свой
    самоподписанный сертификат, чужое имя, просроченный."""

    @classmethod
    def setUpClass(cls):
        if not (LDAP3_PRESENT and _openssl_present()):
            raise unittest.SkipTest("нужны ldap3 и openssl")
        cls.tmp = tempfile.mkdtemp(prefix="ldaps-tls-")
        d = Path(cls.tmp)
        cls.ca_cert = str(d / "ca.pem")
        ca_key = str(d / "ca.key")
        _run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", ca_key, "-out", cls.ca_cert, "-days", "2",
             "-subj", "/CN=Test CA",
             # Без keyUsage современный OpenSSL отвергает сам CA, и тогда
             # «плохие» сценарии проходили бы по неверной причине.
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign")

        def leaf(name: str, cn: str, *extra: str) -> tuple:
            key = str(d / f"{name}.key")
            csr = str(d / f"{name}.csr")
            crt = str(d / f"{name}.pem")
            _run("openssl", "req", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", key, "-out", csr, "-subj", f"/CN={cn}")
            _run("openssl", "x509", "-req", "-in", csr, "-CA", cls.ca_cert,
                 "-CAkey", ca_key, "-out", crt,
                 "-extfile", str(_ext_file(d, name, cn)),
                 *extra)
            return crt, key

        cls.good = leaf("good", "localhost", "-days", "2")
        cls.wrong_name = leaf("wrong", "not-the-server.example", "-days", "2")
        cls.expired = cls._expired_cert(d, ca_key)

        # Самоподписанный: подписан сам собой, а не тестовым CA.
        self_key = str(d / "self.key")
        self_crt = str(d / "self.pem")
        _run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", self_key, "-out", self_crt, "-days", "2",
             "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost")
        cls.self_signed = (self_crt, self_key)

    @classmethod
    def _expired_cert(cls, d: Path, ca_key: str) -> tuple:
        """Сертификат с прошедшим сроком.

        В OpenSSL 3.5 хватает `-not_after`; на 3.0, который стоит на раннерах
        CI, такой опции нет, и дата задаётся через `openssl ca -enddate`. Без
        запасного пути весь класс тестов молча пропускался бы именно там, где
        он и должен работать.
        """
        key = str(d / "expired.key")
        csr = str(d / "expired.csr")
        crt = str(d / "expired.pem")
        ext = str(_ext_file(d, "expired", "localhost"))
        _run("openssl", "req", "-newkey", "rsa:2048", "-nodes",
             "-keyout", key, "-out", csr, "-subj", "/CN=localhost")
        if _supports_not_after():
            _run("openssl", "x509", "-req", "-in", csr, "-CA", cls.ca_cert,
                 "-CAkey", ca_key, "-out", crt, "-extfile", ext,
                 "-not_before", "20200101000000Z",
                 "-not_after", "20200102000000Z")
            return crt, key

        ca_dir = d / "ca-db"
        (ca_dir / "newcerts").mkdir(parents=True)
        (ca_dir / "index.txt").write_text("", encoding="utf-8")
        (ca_dir / "serial").write_text("01\n", encoding="utf-8")
        conf = d / "ca.conf"
        conf.write_text(
            "[ca]\ndefault_ca = CA_default\n\n"
            "[CA_default]\n"
            f"dir = {ca_dir}\n"
            "database = $dir/index.txt\n"
            "new_certs_dir = $dir/newcerts\n"
            "serial = $dir/serial\n"
            "default_md = sha256\n"
            "policy = policy_any\n"
            "email_in_dn = no\n"
            "unique_subject = no\n\n"
            "[policy_any]\ncommonName = supplied\n",
            encoding="utf-8",
        )
        _run("openssl", "ca", "-batch", "-config", str(conf),
             "-cert", cls.ca_cert, "-keyfile", ca_key,
             "-in", csr, "-out", crt, "-extfile", ext,
             "-startdate", "20200101000000Z", "-enddate", "20200102000000Z")
        return crt, key

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "tmp", ""), ignore_errors=True)

    # -- вспомогательное ---------------------------------------------------
    def _serve(self, cert_key: tuple) -> int:
        """Поднимает TLS-сервер на localhost, возвращает порт."""
        cert, key = cert_key
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        def accept():
            try:
                raw, _ = listener.accept()
                try:
                    ctx.wrap_socket(raw, server_side=True).close()
                except Exception:
                    pass
                raw.close()
            except Exception:
                pass
            finally:
                listener.close()

        threading.Thread(target=accept, daemon=True).start()
        self.addCleanup(listener.close)
        return port

    def _connect(self, port: int, server_name: str = "localhost"):
        """Повторяет то, что делает ldap3.Tls.wrap_socket с нашими параметрами:
        цепочку и срок проверяет OpenSSL, имя — отдельная проверка ldap3."""
        import rupochta_server as rs

        tls = rs._ldap_tls()
        ctx = ssl.create_default_context(cafile=tls.ca_certs_file)
        ctx.check_hostname = False
        ctx.verify_mode = tls.validate
        for option in tls.ssl_options:
            ctx.options |= option
        # Тот же пол, что дают ssl_options выше, но выраженный современным
        # API: через options его не видит ни статический анализ, ни читатель.
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            # Рукопожатие падает в половине сценариев — сокет закрываем сами,
            # иначе негативные тесты текут дескрипторами.
            wrapped = ctx.wrap_socket(raw, server_side=False)
        except Exception:
            raw.close()
            raise
        try:
            ldap3_check_hostname(wrapped, server_name, None)
        finally:
            wrapped.close()

    def setUp(self):
        import rupochta_server as rs

        self._saved_ca = rs.CFG.LDAP_CA_FILE
        rs.CFG.LDAP_CA_FILE = self.ca_cert
        self.addCleanup(setattr, rs.CFG, "LDAP_CA_FILE", self._saved_ca)

    # -- сценарии ----------------------------------------------------------
    def test_trusted_certificate_is_accepted(self):
        self._connect(self._serve(self.good))

    def test_self_signed_certificate_is_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError) as caught:
            self._connect(self._serve(self.self_signed))
        self.assertIn("self-signed", str(caught.exception).lower())

    def test_expired_certificate_is_rejected(self):
        with self.assertRaises(ssl.SSLCertVerificationError) as caught:
            self._connect(self._serve(self.expired))
        # Именно из-за срока, а не из-за случайной поломки цепочки.
        self.assertIn("expired", str(caught.exception).lower())

    def test_wrong_hostname_is_rejected(self):
        # Цепочка у этого сертификата верная — режет именно проверка имени,
        # которую ldap3 делает сам после рукопожатия.
        with self.assertRaises(LDAPCertificateError) as caught:
            self._connect(self._serve(self.wrong_name))
        self.assertIn("doesn't match", str(caught.exception))

    def test_validation_is_not_optional(self):
        import rupochta_server as rs

        self.assertEqual(rs._ldap_tls().validate, ssl.CERT_REQUIRED)


class LdapConfigurationTests(unittest.TestCase):
    """Конфигурация не должна оставлять способ подключиться без TLS."""

    def test_every_server_goes_through_the_verified_helper(self):
        """Прямой ldap3.Server в обход помощника снова принесёт CERT_NONE.

        Разбираем AST, а не ищем подстроку: поиск по тексту ломается о пробелы
        и не отличает вызов внутри помощника от вызова где угодно ещё.
        """
        tree = ast.parse(SERVER_SRC)
        # Обходим всё дерево и запоминаем ближайшую функцию для каждого узла:
        # если начинать с определений функций, вызов на уровне модуля
        # (LDAP_SERVER = Server(...)) в проверку не попадёт.
        enclosing = {}

        def mark(node, fn):
            for child in ast.iter_child_nodes(node):
                inner_fn = (
                    child.name
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else fn
                )
                enclosing[child] = inner_fn
                mark(child, inner_fn)

        mark(tree, None)
        outside = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "Server":
                continue
            where = enclosing.get(node)
            if where != "_ldap_server":
                outside.append(f"{where or '<уровень модуля>'}:{node.lineno}")
        self.assertEqual(
            outside, [],
            f"ldap3.Server создаётся в обход _ldap_server: {outside}",
        )

    def test_plaintext_urls_are_dropped(self):
        import rupochta_server as rs

        raw = ["ldaps://dc1.corp.local", "ldap://dc2.corp.local", "LDAP://dc3"]
        keep = [item for item in raw if item.lower().startswith("ldaps://")]
        drop = [item for item in raw if not item.lower().startswith("ldaps://")]
        self.assertEqual(keep, ["ldaps://dc1.corp.local"])
        self.assertEqual(len(drop), 2)
        # Та же логика должна стоять в конфиге, а не только в тесте.
        self.assertIn('item.lower().startswith("ldaps://")', SERVER_SRC)
        self.assertTrue(hasattr(rs.CFG, "LDAP_SERVERS_REJECTED"))

    def test_helper_refuses_plaintext_url(self):
        import rupochta_server as rs

        for bad in ("ldap://dc.corp.local", "ldaps://", "ldaps://dc:notaport"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                rs._ldap_server(bad)

    def test_url_forms_are_parsed(self):
        """Адрес может быть IPv6 в скобках или с хвостом после хоста —
        split(':') на таких формах разъезжается."""
        import rupochta_server as rs

        cases = {
            "ldaps://dc1.corp.local": ("dc1.corp.local", 636),
            "ldaps://dc1.corp.local:1636": ("dc1.corp.local", 1636),
            "ldaps://[2001:db8::1]:636": ("2001:db8::1", 636),
            "ldaps://dc1.corp.local:636/": ("dc1.corp.local", 636),
        }
        for url, (host, port) in cases.items():
            with self.subTest(url=url):
                srv = rs._ldap_server(url)
                self.assertEqual((srv.host, srv.port), (host, port))
                self.assertTrue(srv.ssl)

    def test_ldap_is_unavailable_without_accepted_servers(self):
        """Пароль бинда есть, но все адреса были plaintext — каталог не
        работает, и настройки не должны сообщать обратное."""
        import rupochta_server as rs

        saved = (rs.CFG.LDAP_SERVERS, rs.CFG.LDAP_BIND_PASS)
        try:
            rs.CFG.LDAP_BIND_PASS = "секрет"
            rs.CFG.LDAP_SERVERS = []
            self.assertFalse(rs._ldap_bind_available())
            rs.CFG.LDAP_SERVERS = ["ldaps://dc1.corp.local"]
            self.assertEqual(rs._ldap_bind_available(), rs.LDAP_AVAILABLE)
        finally:
            rs.CFG.LDAP_SERVERS, rs.CFG.LDAP_BIND_PASS = saved

    def test_tls_floor_excludes_obsolete_versions(self):
        import rupochta_server as rs

        options = rs._ldap_tls().ssl_options
        self.assertIn(ssl.OP_NO_TLSv1, options)
        self.assertIn(ssl.OP_NO_TLSv1_1, options)


def _ext_file(directory: Path, name: str, cn: str) -> Path:
    """SAN обязателен: без него современный OpenSSL имя не сверяет."""
    path = directory / f"{name}.ext"
    path.write_text(f"subjectAltName=DNS:{cn}\n", encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
