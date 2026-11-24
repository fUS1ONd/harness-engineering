# Автоматический гейт ревью и авто-мерджа коллабораторских PR

Система проверяет все открытые не-draft PR от контрибьюторов (кроме `fus1ond`), постит отчёт на GitHub и автоматически мерджит PR (`--squash`), если получено `approve`, все CI checks зелёные, нет конфликтов и нет чужих `CHANGES_REQUESTED`.

## Компоненты

- **Детерминированный скрипт:** [scripts/pr-review-gate.py](../scripts/pr-review-gate.py)
  - `scan --lock` — обходит репозитории из `LOCAL_REPOS`, ставит lock на `(repo, pr, head_sha)`, собирает полный контекст с GitHub (PR, комменты, прошлые ревью, inline-комменты, связанные issues + их обсуждения, `parent` + `parent.comments`, `sub_issues`, `sibling_issues`, `referenced_items`).
  - `act` — постит review с маркером `<!-- review-sha:<sha> status:<approved|changes_requested|missing_issue> -->` и при `approve` + зелёном CI мерджит PR.
- **T3 Code webhook-задача:** `Collaborator PR Auto-Review Gate` (`freshSession: true`, авто-`settle` треда после завершения).
- **GitHub Actions триггер:** `.github/workflows/pr-review-trigger.yml` в каждом целевом репозитории (фильтрует `author != 'fus1ond'`, `!draft`, `workflow_run: completed && success`, а также ручной перезапуск комментарием `/review`).

## Как подключить новый репозиторий

> [!IMPORTANT]
> На бесплатном тарифе GitHub **organization-level secrets не передаются в приватные репозитории**. Секреты нужно обязательно ставить на уровне каждого репозитория (`--repo`).

1. **Проставить repository-level секреты в новом репо** (взять значения из существующего репо или при пересоздании webhook-задачи в T3 Code):
   ```bash
   gh secret set T3_PR_GATE_WEBHOOK_URL --repo <org>/<repo> --body "<webhook_url>"
   gh secret set T3_PR_GATE_WEBHOOK_SECRET --repo <org>/<repo> --body "<hmac_secret>"
   ```

2. **Скопировать workflow-триггер** в дефолтную ветку нового репозитория (если PR также открываются в `dev`, добавить файл и в `dev`):
   - Источник: `/root/rumap-core/.github/workflows/pr-review-trigger.yml`
   - Целевой путь: `<repo>/.github/workflows/pr-review-trigger.yml`

3. **Добавить репозиторий в `LOCAL_REPOS`** в [scripts/pr-review-gate.py](../scripts/pr-review-gate.py):
   ```python
   LOCAL_REPOS = {
       "rumap/rumap": "/root/rumap",
       "rumap/rumap-core": "/root/rumap-core",
       "rumap/rumap-android": "/root/rumap-android",
       "<org>/<repo>": "/root/<repo>",
   }
   ```

4. **Проверить локально:**
   ```bash
   python3 /root/harness-engineering/scripts/pr-review-gate.py scan --repos <org>/<repo>
   ```
