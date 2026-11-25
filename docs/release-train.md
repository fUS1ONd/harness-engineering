# Событийный конвейер `release-train` и `prod-smoke` (`rumap/rumap`)

Конвейер автоматически проводит выбранные PR в `rumap/rumap` от мерджа в `dev` до зелёного `main CI`, а после явного подтверждения владельца выполняет поэтапную выкатку на прод с проверкой каждого слоя:

`PR с меткой release-train → dev CI → батчинг в release PR dev → main → main CI → ручное подтверждение «деплой» → center → prod-smoke → worker → prod-smoke → point → prod-smoke`

В работу попадают только PR с явной opt-in меткой `release-train`. Остальные PR живут в обычном режиме.

## Компоненты

- **Стейт-машина `release-train`:** [scripts/release-train.py](../scripts/release-train.py)
  - `scan` — собирает снимок состояния GitHub и локальных замков (открытые PR, `dev CI`, release PR `dev → main`, `main CI`, стоп-краны, последний выкаченный `sha`) и печатает рекомендуемое действие.
  - `step` — выполняет ровно один разрешённый переход с идемпотентным замком в `~/.cache/release-train` (переопределяется через `RELEASE_TRAIN_STATE_DIR` или `--state-dir`). Повторные webhook-события для уже отработанного `(repo, key, sha)` завершаются как `noop`.
  - `deploy` — сверяет стоп-краны и подтверждённый `--sha` с текущим `HEAD` ветки `main`, затем поэтапно выкатывает слои `center → worker → point` с запуском `prod-smoke` после каждого слоя и фиксирует выкаченный `sha`.
- **Прод-верификация `prod-smoke`:** [scripts/prod-smoke.py](../scripts/prod-smoke.py)
  - Read-only CLI для проверки слоёв `center`, `worker`, `point` или `all`: статус контейнеров / `systemd`-юнитов без рестартов за окно наблюдения, health-эндпоинты, отсутствие `ERROR` / `PANIC` / `fatal` в логах, живой heartbeat / метрики.
- **T3 Code webhook-задача:** `Release Train (rumap)` (`freshSession: true`, HMAC-подпись `X-Hub-Signature-256: sha256=<hex>`, дедупликация по ключу `{{body.repo}}#{{body.ref}}` в окне `15` с).
- **GitHub Actions триггер:** `.github/workflows/release-train-trigger.yml` в `rumap/rumap` (присутствует в обеих ветках `main` и `dev`).
  - Реагирует на `pull_request_target` (`[labeled, synchronize, reopened, ready_for_review]`) для не-draft PR в `dev` или release PR `dev → main` с меткой `release-train` и без `hold`.
  - Реагирует на `workflow_run` для workflow `Проверка` (`[completed]`, кроме `cancelled`) на ветках `dev`, `main` и на PR.

## Поведение тредов T3 Code

Webhook-задача запускает короткий одноразовый тред (`freshSession: true`) и действует строго по полю `thread_action` из вывода `scripts/release-train.py`:

- **Промежуточный шаг, `noop` или `paused` (`settle`):** выполнен один переход (мердж PR в `dev`, открытие/обновление/мердж release PR), нечего делать или активен стоп-кран → тред сразу закрывается через `t3_thread_organize(action="settle")`.
- **Красный CI (`ci_failed` → `settle`):** при падении CI на PR, release PR, `dev` или `main` скрипт постит комментарий в соответствующий PR со ссылкой на упавший прогон и списком проверок, после чего тред закрывается (`settle`). Повторные уведомления для того же `sha` отсекаются замком.
- **Зелёный `main CI` (`ready_to_deploy` → `wait_deploy`):** когда `dev` синхронизирован с `main`, `main CI` зелёный для текущего `main_sha` и этот `sha` ещё не выкачен, тред публикует в чат сводку коммитов/PR и **не сеттлится** (`idle`), ожидая явной команды владельца `деплой`.
- **Выкатка после `деплой`:**
  - Запускается `scripts/release-train.py deploy --sha <main_sha>`.
  - Если все 3 слоя (`center → worker → point`) выкачены и `prod-smoke` зелёный → публикуется итоговый отчёт и тред закрывается (`settle`).
  - Если `main` успел уйти вперёд (`sha_moved`), упал `docker compose` или провалился `prod-smoke` на любом слое → **жёсткий fail-fast без авто-отката**: следующие слои и узлы не трогаются, тред остаётся открытым (`alert_open`) с диагностикой и фрагментами логов для решения владельца.

## Батчинг `dev → main`

- Все зелёные PR в `dev` с меткой `release-train` (не draft, без `hold`, без конфликтов, без чужих `CHANGES_REQUESTED`) вливаются в `dev` по готовности (`--squash --delete-branch`).
- Открытие, обновление описания и мердж release PR `dev → main` выполняются только при одновременном выполнении трёх условий:
  1. на `dev` нет активных (`in_progress` / `queued`) прогонов `ci.yml`;
  2. последний завершённый `dev CI` зелёный и относится к текущему `dev_sha`;
  3. `dev` опережает `main` (`ahead_by > 0`).
- Release PR `dev → main` создаётся с меткой `release-train` и списком коммитов/PR в теле; при доливке новых PR в `dev` описание обновляется под новый `dev_sha`.
- Мердж `dev → main` выполняется обычным merge-коммитом (`--merge`), чтобы не разводить историю `main...dev`.

## Стоп-краны

Стоп-краны проверяются перед каждым мутирующим переходом в `step` и перед началом `deploy`:

1. **На уровне PR:** снять метку `release-train` или повесить метку `hold` (работает как на feature PR в `dev`, так и на release PR `dev → main`).
2. **Глобально в репозитории:** переменная репозитория `RELEASE_TRAIN_PAUSED` в `rumap/rumap`. Любое значение, кроме пустого, `0`, `false`, `no`, `off`, замораживает конвейер:
   ```bash
   gh variable set RELEASE_TRAIN_PAUSED --repo rumap/rumap --body "1"
   gh variable delete RELEASE_TRAIN_PAUSED --repo rumap/rumap
   ```
3. **Локально на хосте:** наличие файла `/tmp/release-train.paused` (переопределяется через `RELEASE_TRAIN_PAUSE_FILE` или `--pause-file`):
   ```bash
   touch /tmp/release-train.paused
   rm -f /tmp/release-train.paused
   ```

## Ручной гейт «деплой» и локальные команды

Решение о выкатке на прод всегда остаётся за владельцем: конвейер останавливается на зелёном `main CI` и переходит к выкатке только после команды `деплой` в открытом треде (или ручного запуска `deploy` из терминала).

```bash
# Снимок состояния конвейера и рекомендуемый следующий шаг
python3 /root/harness-engineering/scripts/release-train.py scan --format text

# Выполнить ровно один разрешённый переход
python3 /root/harness-engineering/scripts/release-train.py step --repo rumap/rumap --format both

# Поэтапная выкатка подтверждённого коммита main (center → worker → point)
python3 /root/harness-engineering/scripts/release-train.py deploy --sha <main_sha> --format both

# Отдельная read-only проверка здоровья текущего прода
python3 /root/harness-engineering/scripts/prod-smoke.py --role all --format text
python3 /root/harness-engineering/scripts/prod-smoke.py --role center --window 5m --format both
```

## Настройка и пересоздание

> [!IMPORTANT]
> На бесплатном тарифе GitHub **organization-level secrets не передаются в приватные репозитории**. Секреты webhook нужно обязательно задавать на уровне репозитория (`--repo rumap/rumap`), а не организации.

1. **T3 Code webhook-задача `Release Train (rumap)`:**
   - `freshSession: true`
   - HMAC-подпись: заголовок `X-Hub-Signature-256`, префикс `sha256=`, кодировка `hex`
   - Дедупликация: `coalesceKey: "{{body.repo}}#{{body.ref}}"`, `coalesceWindowSeconds: 15`
   - После создания сохранить выданные `webhookUrl` и HMAC-секрет в секреты репозитория.

2. **Repository-level секреты в `rumap/rumap`:**
   ```bash
   gh secret set T3_RELEASE_TRAIN_WEBHOOK_URL --repo rumap/rumap --body "<webhook_url>"
   gh secret set T3_RELEASE_TRAIN_WEBHOOK_SECRET --repo rumap/rumap --body "<hmac_secret>"
   ```

3. **Метки в `rumap/rumap`:**
   - `release-train` — opt-in включение PR в конвейер;
   - `hold` — стоп-кран на уровне конкретного PR.

4. **Workflow-триггер в обеих ветках (`main` и `dev`):**
   - Путь в `rumap/rumap`: `.github/workflows/release-train-trigger.yml`.
   - Файл должен присутствовать и в `main`, и в `dev`, чтобы корректно отрабатывать события `pull_request_target` и `workflow_run` для обеих веток.
