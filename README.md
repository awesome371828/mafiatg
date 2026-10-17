# Crypto Royale

Crypto Royale — оригинальная аренная PvP-игра для Telegram Mini App: три башни, эликсир, отряды, развитие профиля, трофеи и быстрый матч с живым соперником либо AI. Проект вдохновлён жанром lane-based tower battler, но использует собственные названия и визуальный стиль.

## Состав
- `frontend/` — статический Telegram Mini App, GitHub Pages friendly.
- `backend/main.py` — aiogram 3 + FastAPI/WebSocket + SQLite.
- `backend/requirements.txt` — Python зависимости.
- `.github/workflows/pages.yml` — публикация фронтенда на GitHub Pages.

## Быстрый запуск фронтенда
1. В GitHub откройте **Settings → Pages**.
2. В разделе Build and deployment выберите **GitHub Actions**.
3. После push откройте вкладку **Actions** и дождитесь workflow Pages.
4. URL будет вида `https://awesome371828.github.io/cryptoroyale/`.

## Запуск сервера
Нужен Python 3.10+ и публичный HTTPS/WSS адрес для WebSocket (например, сервер с reverse proxy). GitHub Pages не запускает Python и WebSocket backend.

Windows PowerShell:
```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:TELEGRAM_BOT = "ТОКЕН_ОТ_BOTFATHER"
$env:ADMIN_ID = "6652898792"
$env:WEBAPP_URL = "https://awesome371828.github.io/cryptoroyale/"
$env:PUBLIC_WS_URL = "wss://ВАШ-ДОМЕН/ws"
python main.py
```

Linux:
```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT='ТОКЕН_ОТ_BOTFATHER'
export ADMIN_ID='6652898792'
export WEBAPP_URL='https://awesome371828.github.io/cryptoroyale/'
export PUBLIC_WS_URL='wss://ВАШ-ДОМЕН/ws'
python main.py
```

Порт по умолчанию: 8000. Для production поставьте перед Uvicorn HTTPS reverse proxy и проксируйте WebSocket upgrade. Не помещайте токен бота в frontend или репозиторий.

После размещения backend обязательно отредактируйте `frontend/config.js` перед публикацией фронтенда:

```js
window.CR_API_BASE = "https://api.example.com";
window.CR_WS_URL = "wss://api.example.com/ws";
```

Замените `api.example.com` на ваш собственный домен backend. Используйте HTTPS/WSS; адрес GitHub Pages не может запускать Python и не является WebSocket-сервером. После коммита workflow автоматически опубликует обновлённый frontend.

## Подключение к Telegram
1. Установите `WEBAPP_URL` в URL опубликованной страницы.
2. В BotFather настройте домен Web App, если Telegram попросит.
3. Запустите бота командой `/start`; кнопка откроет Mini App.
4. В настройках Telegram-бота можно назначить ту же ссылку как Menu Button.

## Игровая механика
- Матч идёт на сервере с фиксированным шагом 30 тиков/сек.
- Если соперник не найден за 3 секунды, создаётся AI-соперник.
- Клиент отправляет только намерения (разыграть карту и координату); HP башен, эликсир, урон и исход матча рассчитывает сервер.
- В офлайн-режиме браузер запускает локальную симуляцию для тестирования.
- VIP даёт +50% к регенерации эликсира в серверном матче.

## Админ-команды
В личном чате с ботом, только для `ADMIN_ID`: `/ban ID`, `/unban ID`, `/give_vip ID`, `/stat`.

## Важно
Это самостоятельная игра с оригинальными названиями и простыми геометрическими игровыми юнитами, а не копирование защищённых ассетов, карт, интерфейса или персонажей Clash Royale. SQLite подходит для одного процесса и небольшого MVP; для масштабирования используйте PostgreSQL и общий брокер состояния.
