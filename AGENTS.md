# Главная инструкция (Home Repo)

Ты запущен из личного домашнего репозитория харнесса. Отсюда ведётся планирование, оркестрация и координация работы над проектами.

## Карта проектов и знаний

- **`/root/rumap`** (`rumap/rumap`) — основной кодовый репозиторий (Go: замер, центр, ТГ-бот, воркеры).
  - Архитектура и команды: `/root/rumap/ARCHITECTURE.md`
  - Домен и словарь: `/root/rumap/CONTEXT.md`
  - Стандарты Go-кода и антипаттерны: `/root/rumap/CODING_STANDARDS.md`
  - Трекер и метки (GitHub Issues): `/root/rumap/ISSUE_TRACKER.md`
- **`/root/rumap-core`** (`rumap/rumap-core`) — общее platform-independent Go-ядро (`AGENTS.md`, `CODING_STANDARDS.md`, `Makefile`).
  - Трекер и метки (GitHub Issues): `/root/rumap-core/ISSUE_TRACKER.md`
- **`/root/rumap-android`** (`rumap/rumap-android`) — мобильная точка наблюдения Android (Go JNI + Kotlin/Compose).
  - Архитектура и карта модулей: `/root/rumap-android/ARCHITECTURE.md`
  - Домен и словарь: `/root/rumap-android/GLOSSARY.md`
  - Стандарты Go/Kotlin/JNI: `/root/rumap-android/CODING_STANDARDS.md`
  - Трекер и метки (GitHub Issues): `/root/rumap-android/ISSUE_TRACKER.md`
- **`/root/rumap-hq`** (`fUS1ONd/rumap-hq`) — деловая база `rumap`: метрики, продуктовые решения (`NOW.md`).
- **`/root/research`** — база исследований по всем проектам (правила — `/root/research/README.md`, оглавление — `/root/research/INDEX.md`). Перед новым исследованием проверяй `INDEX.md`.

## Регламент работы (обязательно к чтению по задаче)

- **Оркестрация и делегаты T3 Code:** [workflow/orchestration.md](workflow/orchestration.md)
- **Трекер задач и спеки (`.scratch/`):** [workflow/issue-tracker.md](workflow/issue-tracker.md)
- **Макеты и экраны до кода (`html_render`):** [workflow/ui-preview.md](workflow/ui-preview.md)
- **E2E-стенд проверки VPN-подписок (`ssh home`):** [workflow/vpn-e2e-stand.md](workflow/vpn-e2e-stand.md) (`scripts/vpn-e2e-stand.py`)
- **Агентская верификация приложений (`ssh home`):** [workflow/app-verification.md](workflow/app-verification.md) (`scripts/app-verify.py`)
- **Авто-ревью и мердж коллабораторских PR:** [docs/pr-review-gate.md](docs/pr-review-gate.md) (`scripts/pr-review-gate.py`)
- **Инженерные принципы и ворота качества:** [standards/engineering.md](standards/engineering.md)

## Глобальные правила коммитов и контекста

- Политика комментариев задаётся целевым репозиторием: в репозиториях `rumap`, `rumap-core` и `rumap-android` комментарии в коде запрещены AST-храповиками (кроме машинных директив компилятора/линтера и CGo-преамбулы), а архитектурные решения фиксируются в `docs/adr/`. В остальных проектах комментарии пиши на принятом в репо языке (если не задано — на русском) и только про неочевидное «почему».
- Никогда не указывать авторство ИИ в коммитах, комментариях и PR. Автора коммита не задавать флагами (берётся из `git config`).
- Следить за контекстным окном и 5-часовыми / недельными лимитами через CLI `ctx` (`ctx` — окно текущей сессии + пулы `5h`/`7d`, `ctx -n` — точное число токенов, `ctx <id>` — контекст сабагента/треда). При достижении лимита окна (например, ~200k у оркестратора) передавать работу в новый тред; отработавшие дочерние треды T3 сразу закрывать через `t3_thread_organize{action: "settle"}`.
- Вопросы владельцу по развилкам и интервью (`/grilling`, `/grill-me`, согласование плана/спеки) задавать через `ask_question`: 2–4 лаконичных варианта, первым — рекомендуемый с префиксом `(Recommended) `, без нумерации и без ручного пункта «Другое / напишу в Other» (поле свободного ввода встроено в карточку T3 Code). Тривиальные «да/нет» — коротким текстом в чате.
