from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "rupochta_server.py").read_text(encoding="utf-8")
ADMIN_JS = (ROOT / "static" / "admin.js").read_text(encoding="utf-8")
APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8")

# рупочта.рф в punycode: у зоны верхнего уровня в имени есть цифры и дефисы.
IDN_DOMAIN = "рупочта.рф".encode("idna").decode()
IDN_MAILBOX = f"admin@{IDN_DOMAIN}"
ASCII_MAILBOX = "admin@example.com"


def _server_pattern(anchor: str) -> re.Pattern:
    """Достаёт литерал регулярки из исходника по строке-якорю рядом с ним."""
    line = next(item for item in SERVER.splitlines() if anchor in item)
    body = re.search(r'r"(.+)"|r\'(.+)\'', line.strip())
    assert body, f"не нашёл литерал регулярки в строке: {line!r}"
    return re.compile(body.group(1) or body.group(2))


class IdnMailboxTests(unittest.TestCase):
    """Ящики в IDN-домене (рупочта.рф) доходят до сервера в punycode, а его
    TLD — xn--p1ai — содержит цифры и дефис. Валидация адресов не должна
    требовать TLD из одних букв, иначе такие ящики нельзя ни завести, ни
    прочитать из postfix-accounts.cf."""

    def test_punycode_tld_is_valid(self):
        pattern = _server_pattern("_EMAIL_RE = re.compile(")
        self.assertTrue(pattern.match(IDN_MAILBOX), f"{IDN_MAILBOX} отвергнут _EMAIL_RE")
        self.assertTrue(pattern.match(ASCII_MAILBOX))

    def test_punycode_tld_survives_account_parsing(self):
        # `setup email list` и разбор postfix-accounts.cf — два отдельных места
        # с почти одинаковыми регулярками. Достаём оба литерала разом, иначе
        # поиск по якорю-подстроке дважды находит первый и вторая ветка
        # остаётся непокрытой.
        literals = re.findall(
            r'r"(\(?\[a-z0-9\]\[a-z0-9\._\+\\-\]\*@[^"]+)"',
            SERVER,
        )
        self.assertEqual(
            len(literals), 2,
            "ожидались ровно две регулярки разбора учёток, найдено: "
            f"{len(literals)}",
        )
        for literal in literals:
            pattern = re.compile(literal.strip("()"))
            with self.subTest(literal=literal):
                # Именно fullmatch: search нашёл бы `.xn` внутри адреса и
                # прошёл бы даже со старым шаблоном TLD из одних букв.
                self.assertTrue(pattern.fullmatch(IDN_MAILBOX))
                self.assertTrue(pattern.fullmatch(ASCII_MAILBOX))
                self.assertIsNone(pattern.fullmatch("user@example.x-"))

    def test_admin_ui_accepts_punycode_tld(self):
        line = next(item for item in ADMIN_JS.splitlines() if "const emailMatch = raw.match(" in item)
        literal = re.search(r"/(.+)/\)", line.strip())
        self.assertIsNotNone(literal, "не нашёл регулярку адреса в admin.js")
        pattern = re.compile(literal.group(1))
        self.assertTrue(pattern.search(IDN_MAILBOX))
        self.assertTrue(pattern.search(ASCII_MAILBOX))

    def test_compose_field_accepts_punycode_tld(self):
        # parseEmailList разбирает адреса в полях «Кому»/«Копия».
        line = next(item for item in APP_JS.splitlines() if "const match = raw.match(" in item)
        literal = re.search(r"/(.+)/i\)", line.strip())
        self.assertIsNotNone(literal, "не нашёл регулярку адреса в app.js")
        pattern = re.compile(literal.group(1), re.IGNORECASE)
        self.assertTrue(pattern.search(IDN_MAILBOX))
        self.assertTrue(pattern.search(ASCII_MAILBOX))

    def test_bare_hostname_is_still_rejected(self):
        pattern = _server_pattern("_EMAIL_RE = re.compile(")
        for bad in ("admin@localhost", "admin@", "@example.com", "админ@рупочта.рф"):
            with self.subTest(bad=bad):
                self.assertIsNone(pattern.match(bad))

    def test_hyphen_may_not_edge_the_last_label(self):
        # Расширение под punycode не должно пропускать имена, которые не могут
        # быть хостом: дефис по краям метки запрещён RFC 1123.
        pattern = _server_pattern("_EMAIL_RE = re.compile(")
        for bad in ("user@example.x-", "user@example.-x", "user@example.--"):
            with self.subTest(bad=bad):
                self.assertIsNone(pattern.match(bad))


if __name__ == "__main__":
    unittest.main()
