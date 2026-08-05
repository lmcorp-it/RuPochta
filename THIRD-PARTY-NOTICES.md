# Сторонние компоненты

Лицензия MIT в [LICENSE](LICENSE) распространяется на код самого проекта.
В репозитории есть вендорённые библиотеки с собственными лицензиями:

| Файл | Компонент | Лицензия |
|---|---|---|
| `static/vendor/gsap.min.js` | GSAP 3.15.0, © GreenSock | [GreenSock Standard License](https://gsap.com/standard-license) — бесплатна для большинства применений, но **не** MIT |
| `static/qrcode.min.js` | qrcode.js, © davidshimjs | MIT |

Иллюстрации в `static/brand/` распространяются вместе с проектом на условиях
MIT.

Если условия GreenSock не подходят вашему проекту, удалите
`static/vendor/gsap.min.js` вместе с его подключением в `static/index.html` и
записью в `SHELL_ASSETS` в `static/sw.js` — анимация входа отключится,
остальной интерфейс работает без неё.
