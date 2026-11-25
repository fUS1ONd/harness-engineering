# Автоматический триаж баг-issue (Benny) — триггер, метки и секреты

Система принимает новые баг-репорты и повторно отправленные на триаж issue из репозиториев организации `rumap`, предфильтрует очевидные не-кандидаты и отправляет HMAC-подписанный webhook в T3 Code. Задача `Benny Issue Triage Gate` разбирает issue в режиме read-only, обновляет метки (`area:*` и `status:*`), публикует структурированный отчёт с маркером `<!-- benny-triage -->` и при выполнении жёстких предохранителей запускает авто-фиксер в отдельном worktree.

Шаблон workflow-триггера: [docs/templates/issue-triage-trigger.yml](templates/issue-triage-trigger.yml).

## Компоненты

- **Детерминированный скрипт:** [scripts/issue-triage-gate.py](../scripts/issue-triage-gate.py)
  - `inspect <repo> <issue> [--lock] [--max-fixers 1|2] [--lock-dir ...]` — точечная проверка одного issue (основной путь при доставке webhook с `repo` и `issue`): проверяет фильтры, захватывает атомарный TTL-лок (15 мин) на `(repo, issue_number)`, собирает `referenced_items`, считает активных авто-фиксеров (`status:in-progress` по всем `LOCAL_REPOS`) и отдаёт JSON-контекст.
  - `scan [--lock] [--repos ...] [--max-fixers 1|2] [--lock-dir ...]` — обход репозиториев из `LOCAL_REPOS` (резервный путь при ручном запуске без параметров вебхука). Переиспользует полученный список открытых issue для подсчёта `status:in-progress` по сканируемым репо и опрашивает только оставшиеся.
  - `act <repo> <issue> --body-file <file> --area <area> [--area ...] --status <status> [--remove-label ...] [--claim-autofix] [--max-fixers 1|2] [--dry-run] [--lock-dir ...]` — сначала обновляет метки через `gh issue edit` (снимая `status:needs-triage` и противоположный статус из `status:ready-for-agent` / `status:needs-owner`), затем публикует triage-комментарий с `<!-- benny-triage -->` через `--body-file -` (stdin) и снимает lock. При `--claim-autofix` перепроверяет лимит `max_fixers` и синхронно переводит issue в `status:in-progress` с `--add-assignee @me` до запуска авто-фиксера. `--dry-run` выводит план действий без мутаций и без снятия lock.
- **Фильтры и повторные триггеры:**
  - Жёсткие стоп-условия (блокируют всегда): есть `assignee`, метки `status:in-progress`, `status:review`, `status:blocked`, `contributor`, `human-only`, не-баговые метки (`enhancement`, `question`, `type:epic`, `documentation`, `duplicate`, `invalid`, `wontfix`, `area:roadmap`, префикс `wayfinder:*`) и заголовки `Spec:` / `Map:` (если нет явной метки `bug` или `status:needs-triage`).
  - Пост-триажные статусы (`status:needs-owner`, `status:ready-for-agent`) и маркер `<!-- benny-triage -->` пропускают issue при обычном обходе, но **не блокируют** повторный триаж при свежем комментарии с `/benny` или `/triage` (отдельной строкой, в том числе в многострочном комментарии) или повторном добавлении метки `status:needs-triage` после последнего маркера.
  - Лимит параллельных авто-фиксеров: `--max-fixers` или переменная окружения `ISSUE_TRIAGE_MAX_FIXERS` (допустимо `1` или `2`, по умолчанию `2`).
- **T3 Code webhook-задача:** `Benny Issue Triage Gate` (`Gemini 4 Neon`, `freshSession: true`, отвязана от конкретного треда, дедупликация по `{{body.repo}}#{{body.issue}}` с окном 30 с, подставляет `{{body.repo}}` и `{{body.issue}}` для точечного `inspect`, авто-`settle` треда после завершения).
- **GitHub Actions триггер:** `.github/workflows/issue-triage-trigger.yml` в каждом целевом репозитории. Слушает `issues: [opened, labeled]` и `issue_comment: [created]`, отсекает PR, issue с `assignee` и жёсткие стоп-метки; `status:needs-owner` и `status:ready-for-agent` блокируют только `issues: opened`, а `labeled: status:needs-triage` и комментарии с `/benny` / `/triage` (включая многострочные) проходят в webhook.

## Формат отчёта Benny

Комментарий публикуется на русском языке с обязательными разделами и маркером `<!-- benny-triage -->`:

- `## Первопричина / Диагноз`
- `## Затронутые модули и символы` (с опорой на `FEATURE_MAP.md` / `ARCHITECTURE.md` и путями `file:line`)
- `## Статус воспроизведения`
- `## Уверенность и скоуп` (`confidence: high | medium | low`, `scope: local | cross-cutting | product-decision`)
- `## Рекомендуемые метки` (`area:*` и `status:ready-for-agent` или `status:needs-owner`)
- `## План фикса и верификации` (`Red-тест → Fix → Green` + обязательная верификация по `area:*`)

## Предохранители авто-фиксера

Benny запускает отдельный worktree-тред авто-фиксера через `t3_thread_launch` **только если одновременно выполнены все 4 условия**:

1. Итоговый статус триажа — `status:ready-for-agent`.
2. `confidence == "high"`.
3. `scope == "local"` (не `cross-cutting` и не `product-decision`).
4. Свободен слот авто-фиксера (`active_fixers < max_fixers`, лимит 1–2 на всю организацию).

Перед вызовом `t3_thread_launch` сам Benny синхронно резервирует issue через `act ... --status status:ready-for-agent --claim-autofix` (перевод в `status:in-progress` + `--add-assignee @me`), что исключает гонку слотов и соблюдает `ISSUE_TRACKER.md`. Авто-фиксер работает по короткому баг-пайпу (`Red-тест → Fix → Green` + проверки по `area:*`), открывает PR с `Fixes #<issue>`, переводит issue в `status:review`, проводит 2-осевое ревью (`Standards` + `Spec`), ждёт зелёного CI через `watch_pull_request` и выполняет прямой `gh pr merge --squash --delete-branch` (без `--auto`, так как на бесплатном плане GitHub `allow_auto_merge: false` для приватных репозиториев).

## Единый словарь меток `area:*`

Во всех трёх репозиториях (`rumap/rumap`, `rumap/rumap-android`, `rumap/rumap-core`) заведён одинаковый набор:

| Метка | Цвет | Описание |
| --- | --- | --- |
| `area:ui` | `1d76db` | UI / frontend / Compose screens |
| `area:android` | `0e8a16` | Android app, JNI, platform integration |
| `area:backend` | `5319e7` | Backend services, center, bot, workers |
| `area:core` | `fbca04` | Shared Go core (rumap-core) |
| `area:vpn` | `d93f0b` | VPN / Xray / sing-box / tunnel / routing |

Создание (идемпотентно, `--force` обновляет цвет и описание):

```bash
for repo in rumap/rumap rumap/rumap-android rumap/rumap-core; do
  gh label create "area:ui"      --repo "$repo" --color 1d76db --description "UI / frontend / Compose screens" --force
  gh label create "area:android" --repo "$repo" --color 0e8a16 --description "Android app, JNI, platform integration" --force
  gh label create "area:backend" --repo "$repo" --color 5319e7 --description "Backend services, center, bot, workers" --force
  gh label create "area:core"    --repo "$repo" --color fbca04 --description "Shared Go core (rumap-core)" --force
  gh label create "area:vpn"     --repo "$repo" --color d93f0b --description "VPN / Xray / sing-box / tunnel / routing" --force
done
```

Проверка:

```bash
for repo in rumap/rumap rumap/rumap-android rumap/rumap-core; do
  echo "=== $repo ==="
  gh label list --repo "$repo" --limit 100 --json name -q '.[].name' \
    | grep -E '^area:(ui|android|backend|core|vpn)$' | sort
done
```

## Как подключить репозиторий

> [!IMPORTANT]
> На бесплатном тарифе GitHub **organization-level secrets не передаются в приватные репозитории**. Секреты `T3_ISSUE_TRIAGE_WEBHOOK_URL` и `T3_ISSUE_TRIAGE_WEBHOOK_SECRET` обязательно задаются на уровне каждого репозитория (`--repo`).

1. **Проставить repository-level секреты** (значения — из T3 Code webhook-задачи `Benny Issue Triage Gate`):

   ```bash
   for repo in rumap/rumap rumap/rumap-android rumap/rumap-core; do
     gh secret set T3_ISSUE_TRIAGE_WEBHOOK_URL    --repo "$repo" --body "<webhook_url>"
     gh secret set T3_ISSUE_TRIAGE_WEBHOOK_SECRET --repo "$repo" --body "<hmac_secret>"
   done
   ```

   Проверка:

   ```bash
   for repo in rumap/rumap rumap/rumap-android rumap/rumap-core; do
     echo "=== $repo ==="
     gh secret list --repo "$repo" | grep -E '^T3_ISSUE_TRIAGE_WEBHOOK_(URL|SECRET)'
   done
   ```

   Пока секреты не заданы, workflow остаётся зелёным: шаг печатает `Webhook secrets not configured, skipping.` и выходит с кодом 0.

2. **Скопировать workflow-триггер** в дефолтную ветку (`main`) нового репозитория:
   - Источник: [docs/templates/issue-triage-trigger.yml](templates/issue-triage-trigger.yml)
   - Целевой путь: `<repo>/.github/workflows/issue-triage-trigger.yml`

3. **Создать метки `area:*`** по таблице выше.

4. **Добавить репозиторий в `LOCAL_REPOS`** в [scripts/issue-triage-gate.py](../scripts/issue-triage-gate.py) и проверить локально:
   ```bash
   python3 /root/harness-engineering/scripts/issue-triage-gate.py scan --repos <org>/<repo>
   python3 /root/harness-engineering/scripts/issue-triage-gate.py inspect <org>/<repo> <issue_num>
   ```

## Подпись вебхука

Тело запроса — `{"repo":"<owner/repo>","issue":<num>,"event":"<event_name>","action":"<action>"}`. Подпись передаётся в заголовке `X-Hub-Signature-256: sha256=<hmac-sha256-hex>`, где HMAC считается по телу запроса с секретом `T3_ISSUE_TRIAGE_WEBHOOK_SECRET`:

```bash
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | sed 's/^.* //')
```
