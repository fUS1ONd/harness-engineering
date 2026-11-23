# Harness Engineering

Личный домашний репозиторий для запуска агентской разработки во всех проектах.

Здесь живут:
- карта проектов и рабочих директорий;
- универсальный регламент оркестрации в **T3 Code** ([workflow/orchestration.md](workflow/orchestration.md));
- формат локального трекера задач ([workflow/issue-tracker.md](workflow/issue-tracker.md));
- обязательный процесс визуальной сверки интерфейсов до кода ([workflow/ui-preview.md](workflow/ui-preview.md));
- общие инженерные принципы построения среды для агентов ([standards/engineering.md](standards/engineering.md));
- вспомогательные скрипты харнесса (`scripts/worktree-audit.sh` — поиск забытых влитых worktree и веток `t3/*`; `scripts/vpn-e2e-stand.py` — E2E-стенд проверки VPN-подписок Remnawave на `ssh home`, см. [workflow/vpn-e2e-stand.md](workflow/vpn-e2e-stand.md)).

Продуктовые репозитории хранят только специфику своего кода (`ARCHITECTURE.md`, `CONTEXT.md`, `CODING_STANDARDS.md`, `Makefile`, CI).
