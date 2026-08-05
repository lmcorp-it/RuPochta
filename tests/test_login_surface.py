from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC_ROOT = ROOT / "static" if (ROOT / "static" / "index.html").is_file() else ROOT
SERVER_PATH = ROOT / "rupochta_server.py"


class LoginSurfaceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
        cls.app = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
        cls.admin_index = (STATIC_ROOT / "admin.html").read_text(encoding="utf-8")
        cls.admin_app = (STATIC_ROOT / "admin.js").read_text(encoding="utf-8")
        cls.style = (STATIC_ROOT / "style.css").read_text(encoding="utf-8")
        cls.worker = (STATIC_ROOT / "sw.js").read_text(encoding="utf-8")

    def test_auth_controls_are_present(self):
        for element_id in ("login-form", "telegram-login-btn", "login-password-toggle"):
            self.assertIn(f'id="{element_id}"', self.index)

    def test_every_provider_goes_through_the_central_broker(self):
        """One authorize endpoint, four providers — the same identity as
        auth/my/help. No AD button and no bot-challenge fallback (ADR-0043)."""
        providers = (
            ("yandex", "provider-logo-yandex"),
            ("yandex/mail", "provider-logo-yandex"),
            ("vk", "provider-logo-vk"),
            ("telegram", "provider-logo-telegram"),
        )
        for provider, logo_marker in providers:
            self.assertIn(f'href="/sso/login?provider={provider}"', self.index)
            self.assertIn(logo_marker, self.index)

    def test_the_retired_login_contours_are_gone(self):
        for element_id in (
            "login-ad-btn",
            "telegram-reset-btn",
            "telegram-auth-panel",
            "telegram-auth-cancel",
            "telegram-reset-complete-btn",
        ):
            self.assertNotIn(f'id="{element_id}"', self.index)
        for endpoint in (
            '"/auth/ad/login"',
            '"/auth/telegram/start"',
            '"/auth/telegram/status"',
            '"/auth/telegram/complete"',
            '"/auth/telegram/reset"',
        ):
            self.assertNotIn(endpoint, self.app)
        # The alias-based Telegram entry point belonged to the same contour.
        self.assertNotIn("/sso/login?telegram=1", self.index)

    def test_login_uses_the_rupochta_auth_visual_language(self):
        self.assertIn('class="mail-services-header"', self.index)
        self.assertIn('/static/brand/rupochta-wordmark.svg', self.index)
        self.assertNotIn('/static/brand/mail-header-wordmark.webp', self.index)
        header_rule = self.style.split(".mail-services-header {", 1)[1].split("}", 1)[0]
        self.assertIn("min-height: 76px", header_rule)
        self.assertIn("padding: 0 4vw", header_rule)
        self.assertNotIn("min-height: 87px", self.style)
        self.assertIn('class="login-hero"', self.index)
        self.assertIn("Рабочая почта без хаоса", self.index)
        self.assertIn("/static/brand/mail-login-organized.webp", self.index)
        self.assertIn('data-login-hero-slide="1"', self.index)
        self.assertEqual(self.index.count("data-login-hero-slide="), 1)
        self.assertIn("Войти в почту", self.index)
        self.assertIn("Используйте удобный способ входа", self.index)
        self.assertIn("или войдите с помощью почты", self.index)
        self.assertIn("/static/brand/mail-workspace-background.webp", self.style)
        self.assertIn("© 2026 RuPochta", self.index)
        self.assertIn('class="login-brand-art"', self.index)
        self.assertIn('/static/brand/mail-login-banner.webp', self.index)
        self.assertNotIn('class="login-brand"', self.index)
        self.assertNotIn('class="login-domain"', self.index)
        self.assertIn(
            "RuPochta Mail login reference",
            (STATIC_ROOT / "style.css").read_text(encoding="utf-8"),
        )

    def test_login_hero_uses_local_gsap_and_respects_reduced_motion(self):
        animation = self.index.split("const { gsap }", 1)[1].split("</script>", 1)[0]
        self.assertIn('/static/vendor/gsap.min.js?v=3.15.0', self.index)
        self.assertIn("const slideInterval = 3", self.index)
        self.assertIn("const transitionDuration = 1.2", self.index)
        self.assertIn("const changeAt = (index + 1) * slideInterval", self.index)
        self.assertIn("const start = changeAt - transitionDuration", self.index)
        self.assertIn("defaults: { duration: transitionDuration, ease: \"sine.inOut\" }", self.index)
        self.assertIn("gsap.matchMedia()", self.index)
        self.assertIn('isDesktop: "(min-width: 1121px)"', self.index)
        self.assertIn('reduceMotion: "(prefers-reduced-motion: reduce)"', self.index)
        self.assertIn("timeline.set(current, hiddenState, changeAt).set(next, visibleState, changeAt)", self.index)
        self.assertIn('.to(current, { autoAlpha: 0 }, start)', self.index)
        self.assertIn('.to(next, { autoAlpha: 1 }, start)', self.index)
        for effect in ("xPercent", "yPercent", "rotation", "scale"):
            self.assertNotIn(effect, animation)
        self.assertIn("timeline.kill()", self.index)
        self.assertIn("media.kill()", self.index)
        self.assertNotIn("cdn.jsdelivr.net", self.index)

    def test_password_visibility_control_is_accessible(self):
        self.assertIn('aria-controls="login-password"', self.index)
        self.assertIn('aria-pressed="false"', self.index)
        self.assertIn('password.type = visible ? "text" : "password"', self.index)
        self.assertIn('toggle.setAttribute("aria-pressed", String(!visible))', self.index)

    def test_user_surfaces_reference_only_fresh_mail_artwork(self):
        self.assertNotIn("/static/icons/", self.index)
        self.assertNotIn("mail-assistant.webp", self.index)
        self.assertNotIn("rupochta-logo.png", self.index)
        for asset in (
            "brand/mail-mark.svg",
            "brand/mail-flight.png",
            "brand/mail-login-organized.webp",
            "brand/mail-login-banner.webp",
            "brand/mail-workspace-background.webp",
            "brand/icon-192.png",
            "brand/icon-512.png",
            "brand/rupochta-wordmark.svg",
        ):
            self.assertTrue((STATIC_ROOT / asset).is_file(), asset)
        self.assertNotIn("/static/icons/", self.admin_index)
        self.assertNotIn("/static/icons/", SERVER_PATH.read_text(encoding="utf-8"))

    def test_mailbox_login_exposes_a_busy_state(self):
        self.assertIn('/static/app.js?v=20260803-compose-session1', self.index)
        self.assertIn('submitButton.setAttribute("aria-busy", "true")', self.app)
        self.assertIn('submitButton.removeAttribute("aria-busy")', self.app)
        self.assertIn("submitButton.replaceChildren(...submitContent)", self.app)
        self.assertNotIn("submitButton.innerHTML", self.app)
        self.assertIn("Проверяем доступ…", self.app)

    def test_empty_state_uses_the_supplied_background_with_readable_copy(self):
        self.assertIn('body[data-theme="dark"] .login-screen', self.style)
        self.assertIn(
            'background: url("/static/brand/mail-workspace-background.webp") center bottom / contain no-repeat',
            self.style,
        )
        self.assertIn('body[data-appearance="workspace"] .message-view.empty::before', self.style)
        self.assertIn('opacity: 0.22', self.style)
        self.assertNotIn('background-color: #4d5053', self.style)
        self.assertIn('.message-view.empty .reader-empty-state', self.style)
        self.assertIn('.reader-empty-state .empty-state', self.style)
        self.assertIn(
            'grid-template-columns: var(--mail-list-width, 420px) 6px minmax(0, 1fr)',
            self.style,
        )

    def test_dark_theme_keeps_login_actions_contrasting(self):
        primary_marker = 'body[data-theme="dark"] .login-form .login-submit {'
        self.assertIn(primary_marker, self.style)
        primary_rule = self.style.split(primary_marker, 1)[1].split("}", 1)[0]
        self.assertIn("background: #1f66e5", primary_rule)
        self.assertIn("color: #fff", primary_rule)

        provider_marker = 'body[data-theme="dark"] .social-login .btn {'
        self.assertIn(provider_marker, self.style)
        provider_rule = self.style.split(provider_marker, 1)[1].split("}", 1)[0]
        self.assertIn("color: #f4f8ff", provider_rule)

    def test_the_mailbox_password_remains_the_only_fallback(self):
        """Password login stays as the fallback for a mailbox with no binding;
        every other legacy fallback is gone (ADR-0043)."""
        self.assertIn('id="login-email"', self.index)
        self.assertIn('id="login-password"', self.index)
        self.assertIn('"/auth/login"', self.app)

    def test_service_worker_uses_the_current_surface_cache_revision(self):
        self.assertIn("rupochta-mail-v27-transparent-banner", self.worker)
        self.assertIn("register('/sw.js?v=20260803-login-banner3'", self.index)
        for asset in (
            "/static/workspace.css?v=20260618c",
            "/static/style.css?v=20260803-login-banner2",
            "/static/focus.css?v=20260618c",
            "/static/app.js?v=20260803-compose-session1",
            "/static/vendor/gsap.min.js?v=3.15.0",
        ):
            self.assertIn(asset, self.worker)
        self.assertIn("/static/brand/mail-login-organized.webp", self.worker)
        self.assertIn("/static/brand/mail-login-banner.webp?v=20260803-transparent1", self.worker)
        self.assertIn("/static/brand/mail-workspace-background.webp", self.worker)
        self.assertIn("/static/brand/rupochta-wordmark.svg", self.worker)
        self.assertNotIn("/static/brand/portaldesk-wordmark.png", self.worker)

    def test_workspace_appearance_and_canonical_navigation_contract(self):
        self.assertIn('<body data-appearance="workspace">', self.index)
        self.assertIn('data-settings-tab="appearance"', self.index)
        self.assertIn('data-appearance-option="workspace"', self.index)
        self.assertIn('data-appearance-option="classic"', self.index)
        self.assertIn('const APPEARANCE_STORAGE_KEY = "rupochta-mail-appearance"', self.app)
        self.assertIn('href="https://mail.example.com/"', self.index)
        self.assertIn(
            '<a class="brand" href="https://mail.example.com/" aria-label="RuPochta Mail — на главную"><img',
            self.index,
        )
        self.assertIn('id="account-modal"', self.index)
        self.assertIn('aria-controls="account-modal"', self.index)
        self.assertIn('apiFetch("/account")', self.app)
        self.assertIn("intent=link&surface=mail", self.app)
        self.assertNotIn('href="https://my.example.com/account"', self.index)
        self.assertNotIn('id="personal-profile-modal"', self.index)
        self.assertNotIn("refreshPersonalProfileCard", self.app)
        self.assertNotIn("BookStack", self.app)
        for sidebar_item in (
            'class="section-title tools-title"',
            'id="feature-actions"',
            'class="section-title services-title"',
            'class="service-links"',
            'aria-label="PortalDesk"',
            '>Поддержка</a>',
            '>Документация</a>',
        ):
            self.assertNotIn(sidebar_item, self.index)
        self.assertNotIn('btn.id = "btn-scheduled"', self.app)
        self.assertNotIn('btn.id = "btn-snoozed"', self.app)

    def test_inbox_tools_are_collapsed_until_requested(self):
        self.assertIn('id="btn-mail-tools-toggle"', self.index)
        self.assertIn('aria-expanded="false" aria-controls="mail-tools"', self.index)
        self.assertIn('id="mail-tools" class="mail-tools hidden"', self.index)
        self.assertIn('function setMailToolsOpen(open)', self.app)
        self.assertIn('if (count > 0) setMailToolsOpen(true)', self.app)
        self.assertIn('.message-list-pane .pane-kicker', self.style)

    def test_sidebar_exposes_standard_mail_folders(self):
        for label in ("Исходящие", "Отправленные", "Все", "Спам"):
            self.assertIn(f'label: "{label}"', self.app)
        self.assertIn('openScheduledList();', self.app)
        self.assertIn('allInbox: !all', self.app)
        self.assertIn('placeholder: !sent', self.app)
        self.assertIn('placeholder: !spam', self.app)

    def test_new_messages_and_replies_use_the_full_workspace_branded_composer(self):
        self.assertIn('class="compose-settings"', self.index)
        self.assertIn('class="compose-editor"', self.index)
        self.assertIn('workspace.appendChild(compose)', self.app)
        self.assertIn('classList.remove("show-sidebar", "show-reader")', self.app)
        self.assertIn('setComposeOpen(true);', self.app)
        self.assertIn('openCompose("reply")', self.app)
        self.assertIn('openCompose("reply_all")', self.app)
        self.assertIn('#compose-modal {', self.style)
        self.assertIn('.mail-workspace {\n  position: relative;', self.style)
        self.assertIn('grid-template-columns: var(--mail-list-width, 420px) minmax(0, 1fr);', self.style)
        self.assertIn('grid-template-columns: minmax(240px, 42%) minmax(0, 1fr);', self.style)
        self.assertIn('.compose-settings .compose-tool-group {', self.style)
        self.assertIn('.compose-editor #compose-body {', self.style)
        self.assertIn('inset: 56px 0 calc(64px + env(safe-area-inset-bottom));', self.style)
        self.assertIn('position: absolute;', self.style)
        compose_style = self.style.split('#compose-modal > .compose-card {', 1)[1].split('.settings-card {', 1)[0]
        self.assertIn('background-image: var(--compose-template-image);', compose_style)
        self.assertIn('background: var(--compose-template-surface', compose_style)
        self.assertIn('backgroundUrl: "https://mail.example.com/static/brand/mail-workspace-background.webp"', self.app)

    def test_compose_uses_the_default_brand_template_and_right_column_subject(self):
        settings = self.index.split('<div class="compose-settings">', 1)[1].split('<div class="compose-editor">', 1)[0]
        editor = self.index.split('<div class="compose-editor">', 1)[1].split('<div class="modal-footer">', 1)[0]
        self.assertNotIn('id="compose-subject"', settings)
        self.assertLess(editor.index('id="compose-subject"'), editor.index('id="compose-body"'))
        self.assertGreater(editor.index('id="btn-compose-add-attachment"'), editor.index('id="compose-body"'))
        self.assertLess(settings.index('id="compose-to"'), settings.index('id="compose-template-select"'))
        self.assertLess(settings.index('id="compose-cc"'), settings.index('id="compose-template-select"'))
        self.assertIn('class="compose-brand-logo"', editor)
        self.assertLess(editor.index('class="compose-template-canvas"'), editor.index('id="compose-body"'))
        self.assertLess(editor.index('id="compose-body"'), editor.index('class="compose-brand-logo"'))
        self.assertLess(editor.index('class="compose-brand-logo"'), editor.index('id="btn-compose-add-attachment"'))
        self.assertIn('RuPochta Mail · по умолчанию', self.app)
        self.assertIn('С уважением,', self.app)
        self.assertIn('buildBrandedEmailHtml(bodyHtml)', self.app)
        self.assertEqual(self.app.count('appendComposeBody(form'), 4)

    def test_external_navigation_suspends_and_resumes_the_same_compose_session(self):
        self.assertNotIn('document.addEventListener("click", (event) => {\n    if (compose.classList.contains("hidden")', self.app)
        self.assertIn('function suspendCompose()', self.app)
        self.assertIn('state.compose.suspended = true;', self.app)
        self.assertIn('function openOrResumeCompose()', self.app)
        self.assertIn('async function preserveComposeBeforeContextExit()', self.app)
        self.assertIn('if (!(await preserveComposeBeforeContextExit())) return;', self.app)
        self.assertIn('if (!options.keepCompose) suspendCompose();', self.app)
        self.assertIn('const items = [...$$(selector)].filter(', self.app)
        self.assertIn('openAccessibleModal(id, trigger = null)', self.app)
        self.assertIn('$("#btn-compose").addEventListener("click", openOrResumeCompose);', self.app)
        suspend_body = self.app.split('function suspendCompose()', 1)[1].split('function closeComposeSession()', 1)[0]
        self.assertNotIn('state.compose.attachments = []', suspend_body)

    def test_brand_preview_and_outgoing_html_share_one_template_contract(self):
        editor = self.index.split('<div class="compose-editor">', 1)[1].split('<div class="modal-footer">', 1)[0]
        self.assertIn('const BRAND_TEMPLATE = Object.freeze({', self.app)
        self.assertIn('syncBrandedComposeTemplate();', self.app)
        self.assertIn('BRAND_TEMPLATE.backgroundUrl', self.app)
        self.assertIn('BRAND_TEMPLATE.logoUrl', self.app)
        self.assertNotIn('src="/static/brand/rupochta-wordmark.svg"', editor)

    def test_compose_supports_safe_rich_text_and_resizable_inline_images(self):
        self.assertIn('contenteditable="true"', self.index)
        for command in ("bold", "italic", "underline", "insertUnorderedList", "insertOrderedList", "createLink"):
            self.assertIn(f'data-compose-command="{command}"', self.index)
        self.assertIn('id="compose-inline-image-input"', self.index)
        self.assertIn('resize: horizontal;', self.style)
        self.assertIn('function sanitizeComposeHtml(root)', self.app)
        self.assertIn('event.clipboardData?.files', self.app)
        self.assertIn('["ArrowLeft", "ArrowRight"]', self.app)
        self.assertIn('form.append("body_text", bodyText);', self.app)
        self.assertNotIn('$("#compose-body").value', self.app)

    def test_server_embeds_inline_editor_images_as_cid_parts(self):
        server = SERVER_PATH.read_text(encoding="utf-8")
        self.assertIn('def _extract_inline_images(', server)
        self.assertIn('html_part.add_related(', server)
        self.assertIn('return f"cid:{cid}"', server)

    def test_sidebar_has_archive_and_inline_folder_creation(self):
        self.assertIn('archive: ["Archive", "Archives"', self.app)
        self.assertIn('label: "Архив"', self.app)
        self.assertIn('id="sidebar-folder-create"', self.index)
        self.assertIn('id="sidebar-folder-name"', self.index)
        self.assertIn('createSidebarFolder', self.app)

    def test_settings_use_a_vertical_menu(self):
        vertical = self.style.split('/* Settings use one stable vertical menu', 1)[1]
        self.assertIn('grid-template-columns: clamp(138px, 24vw, 210px)', vertical)
        self.assertIn('flex-direction: column;', vertical)
        self.assertIn('border-left: 3px solid transparent;', vertical)

    def test_bootstrap_refreshes_the_synced_profile_before_rendering_the_avatar(self):
        server = SERVER_PATH.read_text(encoding="utf-8")
        start = server.index('@app.get("/api/bootstrap")')
        end = server.index('\n\n@app.get("/api/folders")', start)
        bootstrap = server[start:end]
        self.assertIn('await _refresh_portal_profile_card(', bootstrap)
        self.assertIn('"profile": _profile_payload_from_session(sess)', bootstrap)
        self.assertIn('renderUserProfileAvatar(state.me.profile_card)', self.app)
        self.assertEqual(self.app.count('loadAccount().catch(() => {});'), 1)
        account_renderer = self.app.split('function renderAccount(payload = {}) {', 1)[1].split('\n}\n\nasync function loadAccount', 1)[0]
        self.assertIn('$("#user-profile-avatar")', account_renderer)

    def test_multipart_compose_parser_is_installed(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("python-multipart>=", requirements)

    def test_user_surfaces_do_not_expose_legacy_branding(self):
        surfaces = (self.index, self.app, self.admin_index, self.admin_app)
        for legacy_text in (
            "YobDesk",
            "Jimmy",
            "jimmy",
            "yobdesk",
            "lets-mobile",
            "lm.local",
        ):
            for surface in surfaces:
                self.assertNotIn(legacy_text, surface)

    def test_signed_in_official_telegram_bot_link_is_present(self):
        self.assertIn('id="telegram-auth-bot-link"', self.index)
        self.assertIn('href="https://t.me/RuPochta_bot"', self.index)
        self.assertIn("@RuPochta_bot", self.index)
        self.assertIn("Официальный бот авторизации RuPochta.", self.index)
        self.assertNotIn("Открыть Telegram в SSO", self.index)
        self.assertNotIn("function openTelegramSsoAccount()", self.app)


if __name__ == "__main__":
    unittest.main()
