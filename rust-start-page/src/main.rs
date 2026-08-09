// RuPochta — стартовая страница (вход + регистрация) на Rust / Axum.
// Реализация повторяет структуру оригинального сервиса (FastAPI):
//   GET  /                     — страница входа и регистрации
//   GET  /health, /ready       — проверки живости (как в репозитории)
//   GET  /api/signup/config    — настройки регистрации (включена ли, домен, мин. длина пароля)
//   POST /api/signup           — создание ящика (валидация на сервере)
//   POST /api/login            — вход (валидация на сервере)
// Конфигурация через переменные окружения:
//   RUPOCHTA_BIND          адрес прослушивания (по умолчанию 0.0.0.0:8080)
//   RUPOCHTA_DOMAIN        домен ящиков (по умолчанию example.com)
//   RUPOCHTA_SIGNUP_ENABLED включена ли регистрация (1/0, по умолчанию 1)
//   RUPOCHTA_MIN_PASSWORD  минимальная длина пароля (по умолчанию 10)

use axum::{
    extract::State,
    http::StatusCode,
    response::{Html, IntoResponse},
    routing::{get, post},
    Json, Router,
};
use serde::{Deserialize, Serialize};
use std::{net::SocketAddr, sync::Arc};

const PAGE_HTML: &str = include_str!("../static/index.html");
const CANVAS_HTML: &str = include_str!("../static/canvas.html");
const SERVICE_NAME: &str = "rupochta-web";
const VERSION: &str = env!("CARGO_PKG_VERSION");

#[derive(Clone)]
struct AppState {
    domain: Arc<str>,
    signup_enabled: bool,
    min_password: usize,
}

// ---------------------------------------------------------------------------
// Запросы / ответы API
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct StatusBody {
    status: &'static str,
    service: &'static str,
    version: &'static str,
}

async fn health() -> Json<StatusBody> {
    Json(StatusBody { status: "ok", service: SERVICE_NAME, version: VERSION })
}

async fn ready() -> Json<StatusBody> {
    // Демо-режим: сервис всегда готов. В проде здесь проверялись бы подключения
    // к почтовым серверам/каталогу.
    Json(StatusBody { status: "ready", service: SERVICE_NAME, version: VERSION })
}

#[derive(Serialize)]
struct SignupConfig {
    enabled: bool,
    provisioning_ready: bool,
    domain: String,
    min_password: usize,
}

async fn signup_config(State(st): State<AppState>) -> Json<SignupConfig> {
    Json(SignupConfig {
        enabled: st.signup_enabled,
        provisioning_ready: st.signup_enabled,
        domain: st.domain.to_string(),
        min_password: st.min_password,
    })
}

#[derive(Deserialize)]
struct SignupReq {
    login: String,
    password: String,
    password2: String,
}

#[derive(Serialize)]
struct FieldErrors {
    login: Option<String>,
    password: Option<String>,
    password2: Option<String>,
}

#[derive(Serialize)]
struct SignupResp {
    ok: bool,
    email: Option<String>,
    errors: Option<FieldErrors>,
}

/// Имя ящика: 3–30 символов, латиница, цифры, точка/дефис/подчёркивание.
fn validate_login_name(raw: &str) -> Result<String, String> {
    let login = raw.trim().to_lowercase();
    let n = login.len();
    if !(3..=30).contains(&n) {
        return Err("Некорректное имя ящика: 3–30 символов, латиница и цифры.".into());
    }
    if !login.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '.' || c == '-' || c == '_') {
        return Err("Некорректное имя ящика: 3–30 символов, латиница и цифры.".into());
    }
    Ok(login)
}

fn validate_password(raw: &str, min: usize) -> Result<String, String> {
    let pw = raw.to_string();
    if pw.chars().count() < min {
        return Err(format!("Пароль должен быть не короче {} символов.", min));
    }
    Ok(pw)
}

fn validate_password_match(p1: &str, p2: &str) -> Result<(), String> {
    if p1 != p2 {
        return Err("Пароли не совпадают.".into());
    }
    Ok(())
}

async fn signup(State(st): State<AppState>, Json(req): Json<SignupReq>) -> impl IntoResponse {
    if !st.signup_enabled {
        return (
            StatusCode::FORBIDDEN,
            Json(SignupResp {
                ok: false,
                email: None,
                errors: Some(FieldErrors {
                    login: Some("Регистрация на этом сервере отключена администратором.".into()),
                    password: None,
                    password2: None,
                }),
            }),
        );
    }

    let mut errors = FieldErrors { login: None, password: None, password2: None };
    let mut has_error = false;

    match validate_login_name(&req.login) {
        Ok(l) => {
            let _ = l;
        }
        Err(e) => {
            errors.login = Some(e);
            has_error = true;
        }
    }
    match validate_password(&req.password, st.min_password) {
        Ok(_) => {}
        Err(e) => {
            errors.password = Some(e);
            has_error = true;
        }
    }
    if let Err(e) = validate_password_match(&req.password, &req.password2) {
        errors.password2 = Some(e);
        has_error = true;
    }

    if has_error {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(SignupResp { ok: false, email: None, errors: Some(errors) }),
        );
    }

    let email = format!("{}@{}", validate_login_name(&req.login).unwrap_or_default(), st.domain);
    (StatusCode::OK, Json(SignupResp { ok: true, email: Some(email), errors: None }))
}

#[derive(Deserialize)]
struct LoginReq {
    login: String,
    password: String,
    remember: Option<bool>,
}

#[derive(Serialize)]
struct LoginErrors {
    login: Option<String>,
    password: Option<String>,
}

#[derive(Serialize)]
struct LoginResp {
    ok: bool,
    errors: Option<LoginErrors>,
}

async fn login(Json(req): Json<LoginReq>) -> impl IntoResponse {
    let mut errors = LoginErrors { login: None, password: None };
    let mut has_error = false;

    if req.login.trim().is_empty() {
        errors.login = Some("Укажите рабочий email или логин.".into());
        has_error = true;
    }
    if req.password.is_empty() {
        errors.password = Some("Введите пароль.".into());
        has_error = true;
    }

    if has_error {
        return (
            StatusCode::UNPROCESSABLE_ENTITY,
            Json(LoginResp { ok: false, errors: Some(errors) }),
        );
    }
    // Демо: в проде здесь проверка учётных данных через IMAP/OIDC/LDAP.
    let _ = req.remember;
    (StatusCode::OK, Json(LoginResp { ok: true, errors: None }))
}

// ---------------------------------------------------------------------------
// Сборка приложения
// ---------------------------------------------------------------------------

fn app(state: AppState) -> Router {
    Router::new()
        .route("/", get(index))
        .route("/canvas", get(canvas))
        .route("/health", get(health))
        .route("/ready", get(ready))
        .route("/api/signup/config", get(signup_config))
        .route("/api/signup", post(signup))
        .route("/api/login", post(login))
        .layer(tower_http::cors::CorsLayer::permissive())
        .with_state(state)
}

async fn index() -> Html<&'static str> {
    Html(PAGE_HTML)
}

/// GET /canvas — бесконечный холст: контекст проекта, дизайн-система, макеты.
async fn canvas() -> Html<&'static str> {
    Html(CANVAS_HTML)
}

fn state_from_env() -> AppState {
    let domain = std::env::var("RUPOCHTA_DOMAIN").unwrap_or_else(|_| "example.com".into());
    let signup_enabled = std::env::var("RUPOCHTA_SIGNUP_ENABLED")
        .map(|v| v == "1" || v.eq_ignore_ascii_case("true"))
        .unwrap_or(true);
    let min_password = std::env::var("RUPOCHTA_MIN_PASSWORD")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(10);
    AppState { domain: Arc::from(domain), signup_enabled, min_password }
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "rupochta_web=info,tower_http=info".into()),
        )
        .init();

    let bind = std::env::var("RUPOCHTA_BIND").unwrap_or_else(|_| "0.0.0.0:8080".into());
    let addr: SocketAddr = bind.parse().expect("RUPOCHTA_BIND должен быть вида 0.0.0.0:8080");

    let state = state_from_env();
    let app = app(state);
    let listener = tokio::net::TcpListener::bind(addr).await.expect("не удалось открыть порт");

    tracing::info!("RuPochta стартовая страница запущена на http://{addr}");
    axum::serve(listener, app).await.expect("ошибка сервера");
}

// ---------------------------------------------------------------------------
// Тесты
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use axum::body::Body;
    use axum::http::{Request, StatusCode};
    use http_body_util::BodyExt;
    use tower::ServiceExt;

    fn test_state() -> AppState {
        AppState { domain: Arc::from("example.com"), signup_enabled: true, min_password: 10 }
    }

    #[test]
    fn login_name_valid() {
        assert!(validate_login_name("ivan.petrov").is_ok());
        assert!(validate_login_name("a1_b-c").is_ok());
    }

    #[test]
    fn login_name_invalid() {
        assert!(validate_login_name("iv").is_err()); // слишком короткое
        assert!(validate_login_name("иван").is_err()); // кириллица
        assert!(validate_login_name("a b").is_err()); // пробел
        assert!(validate_login_name(&"a".repeat(31)).is_err()); // слишком длинное
    }

    #[test]
    fn password_rules() {
        assert!(validate_password("0123456789", 10).is_ok());
        assert!(validate_password("123456789", 10).is_err());
        assert!(validate_password_match("abc", "abd").is_err());
        assert!(validate_password_match("abc", "abc").is_ok());
    }

    async fn body_text(resp: axum::response::Response) -> String {
        let bytes = resp.into_body().collect().await.unwrap().to_bytes();
        String::from_utf8(bytes.to_vec()).unwrap()
    }

    #[tokio::test]
    async fn health_ok() {
        let resp = app(test_state())
            .oneshot(Request::builder().uri("/health").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
    }

    #[tokio::test]
    async fn index_serves_page() {
        let resp = app(test_state())
            .oneshot(Request::builder().uri("/").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let text = body_text(resp).await;
        assert!(text.contains("Войти в почту"));
        assert!(text.contains("Создать ящик"));
    }

    #[tokio::test]
    async fn canvas_serves_design_package() {
        let resp = app(test_state())
            .oneshot(Request::builder().uri("/canvas").body(Body::empty()).unwrap())
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let text = body_text(resp).await;
        assert!(text.contains("RuПочта — стартовая страница"));
        assert!(text.contains("02 ДИЗАЙН-СИСТЕМА"));
    }

    #[tokio::test]
    async fn signup_valid() {
        let body = serde_json::json!({
            "login": "ivan.petrov",
            "password": "0123456789",
            "password2": "0123456789"
        });
        let resp = app(test_state())
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/signup")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);
        let text = body_text(resp).await;
        assert!(text.contains("\"ok\":true"));
        assert!(text.contains("ivan.petrov@example.com"));
    }

    #[tokio::test]
    async fn signup_invalid_password() {
        let body = serde_json::json!({
            "login": "ivan.petrov",
            "password": "short",
            "password2": "short"
        });
        let resp = app(test_state())
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/signup")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
        let text = body_text(resp).await;
        assert!(text.contains("не короче 10 символов"));
    }

    #[tokio::test]
    async fn signup_mismatch() {
        let body = serde_json::json!({
            "login": "ivan.petrov",
            "password": "0123456789",
            "password2": "0123456780"
        });
        let resp = app(test_state())
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/signup")
                    .header("content-type", "application/json")
                    .body(Body::from(body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        let text = body_text(resp).await;
        assert!(text.contains("Пароли не совпадают"));
    }

    #[tokio::test]
    async fn login_valid_and_invalid() {
        let ok_body = serde_json::json!({ "login": "user@example.com", "password": "x" });
        let resp = app(test_state())
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/login")
                    .header("content-type", "application/json")
                    .body(Body::from(ok_body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::OK);

        let bad_body = serde_json::json!({ "login": "", "password": "" });
        let resp = app(test_state())
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/api/login")
                    .header("content-type", "application/json")
                    .body(Body::from(bad_body.to_string()))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(resp.status(), StatusCode::UNPROCESSABLE_ENTITY);
    }
}
