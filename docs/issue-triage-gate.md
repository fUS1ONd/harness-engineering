# Автоматический триаж баг-issue (Benny) — триггер, метки и секреты

Система принимает новые баг-репорты и issue с `status:needs-triage` из репозиториев организации `rumap`, предфильтрует очевидные не-кандидаты и отправляет HMAC-подписанный webhook в T3 Code. Задача Benny разбирает issue, постит структурированный отчёт с маркером `<!-- benny-triage -->`, проставляет метки `area:*` и переводит статус в `status:ready-for-agent` или `status:needs-owner`.

Шаблон workflow-триггера: [docs/templates/issue-triage-trigger.yml](templates/issue-triage-trigger.yml). Спецификация: [.scratch/benny-issue-triage/spec.md](../.scratch/benny-issue-triage/spec.md).

## Компоненты

- **Детерминированный скрипт:** `scripts/issue-triage-gate.py` (тикет 01).
  - `scan --lock` — обходит репозитории из `LOCAL_REPOS`, фильтрует eligible issues (assignee, стоп-метки, идемпотентность `<!-- benny-triage -->`, повторные триггеры `/benny` / `/triage` / свежий `status:needs-triage`), ставит TTL-lock на `(repo, issue_number)`, считает активных авто-фиксеров против лимита (1–2) и выдаёт структурированный JSON-контекст.
  - `act` — постит triage-комментарий с `<!-- benny-triage -->`, ставит `area:*` и статус, снимает lock; поддерживает `--dry-run`.
- **T3 Code webhook-задача:** Benny (`Gemini 4 Neon`, `freshSession: true`, авто-`settle` треда после завершения).
- **GitHub Actions триггер:** `.github/workflows/issue-triage-trigger.yml` в каждом целевом репозитории. Слушает `issues: [opened, labeled]` и `issue_comment: [created]`, отсекает PR, issue с assignee и стоп-метками, и вызывает webhook только на `opened`, `labeled` с `status:needs-triage` или комментарий, начинающийся с `/benny` / `/triage`.

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

1. **Проставить repository-level секреты** (значения — из T3 Code webhook-задачи Benny, см. тикет 03):

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

4. **Добавить репозиторий в `LOCAL_REPOS`** в `scripts/issue-triage-gate.py` (тикет 01) и проверить локально:
   ```bash
   python3 /root/harness-engineering/scripts/issue-triage-gate.py scan --repos <org>/<repo>
   ```

## Подпись вебхука

Тело запроса — `{"repo":"<owner/repo>","issue":<num>,"event":"<event_name>","action":"<action>"}`. Подпись передаётся в заголовке `X-Hub-Signature-256: sha256=<hmac-sha256-hex>`, где HMAC считается по телу запроса с секретом `T3_ISSUE_TRIAGE_WEBHOOK_SECRET`:

```bash
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | sed 's/^.* //')
```
