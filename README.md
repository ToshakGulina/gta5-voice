# GTA 5 Voice Upgrade Workspace

Рабочий репозиторий для улучшения русской озвучки **Nerual Dreming v3.0** под **GTA V Legacy** (с последующим запуском на ПК и телефоне).

## Состав репозитория
- `tools/gta5_awc_tool.py` — чистый бинарный распаковщик/упаковщик контейнеров `.awc` (Single-Channel и Multi-Channel) с исправлением 5 багов `CodeWalker` (убран щелчок IMA ADPCM в конце фраз, исправлен расчёт `Samples`, сохранены бинарные чанки лицевой анимации `animation` / липсинка и жестов `gesture`).
- `scripts/step1_inspect.ps1` и `step1_inspect.bat` — скрипт автоматической проверки структуры скачанного архива `NeuroDub_GTAV_FREE_dub` и папки игры `GTA V Legacy`.
- `reports/` — папка с автоматическими отчётами для ИИ-ассистента.
