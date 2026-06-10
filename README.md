# AmneziaWG Telegram Admin Bot

Telegram-бот для администрирования `amneziawg-web` через HTTP API.

Бот позволяет:

- просматривать клиентов AmneziaWG;
- создавать клиентов;
- получать исправленный `.conf`;
- получать QR-код конфига;
- включать и отключать клиентов;
- удалять клиентов;
- редактировать отображаемое имя и комментарий;
- смотреть трафик клиента и сервера;
- задавать лимиты трафика;
- управлять DNS-пресетами;
- управлять параметрами `I1-I5`;
- вести аудит действий администраторов;
- ограничивать доступ к боту по Telegram ID.

## Важные ограничения

Бот работает только через HTTP API `amneziawg-web`.

Бот не работает напрямую с:

- WireGuard;
- AmneziaWG kernel/backend;
- файлами конфигурации на диске;
- системными интерфейсами.

Файлы `.conf` на диске бот не изменяет.  
Изменения применяются только к копии конфига, которую бот отправляет в Telegram:

- замена DNS;
- удаление IPv6 из `Address`, если включено в настройках;
- добавление `I1-I5` после строки `H4`.

Сброс статистики в боте очищает только SQLite-статистику самого бота.  
Статистика `amneziawg-web` через API не сбрасывается.

## Требования

- Linux-сервер;
- Python 3.10 или новее;
- установленный и работающий `amneziawg-web`;
- доступный HTTP API `amneziawg-web`;
- Telegram Bot Token;
- Telegram ID владельца бота.

Проверка версии Python:

```bash
python3 --version
```

## Структура проекта

Рекомендуемая структура:

```text
/root/TGbots/amneziawg-telegram-bot/
├── bot.py
├── .env
├── requirements.txt
└── data/
    └── bot.sqlite3
```

## Установка

### 1. Создайте директорию проекта

```bash
mkdir -p /root/TGbots/amneziawg-telegram-bot/data
cd /root/TGbots/amneziawg-telegram-bot
```

### 2. Создайте виртуальное окружение

```bash
python3 -m venv .venv
```

Активируйте его:

```bash
source .venv/bin/activate
```

### 3. Создайте `requirements.txt`

Создайте файл:

```bash
nano requirements.txt
```

Содержимое:

```text
aiogram>=3.0.0
aiohttp>=3.8.0
python-dotenv>=1.0.0
qrcode[pil]>=7.4.0
Pillow>=9.0.0
```

Установите зависимости:

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Поместите код бота

Создайте файл:

```bash
nano bot.py
```

Вставьте в него актуальный код `bot.py`.

## Настройка Telegram-бота

### 1. Создайте бота через BotFather

В Telegram откройте:

```text
@BotFather
```

Создайте нового бота:

```text
/newbot
```

Скопируйте токен вида:

```text
123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Это значение понадобится для `BOT_TOKEN`.

### 2. Узнайте свой Telegram ID

Можно использовать, например:

```text
@userinfobot
```

Нужен числовой Telegram ID, например:

```text
123456789
```

Это значение понадобится для `BOOTSTRAP_OWNER_ID`.

## Настройка `.env`

Создайте файл:

```bash
nano .env
```

Пример содержимого:

```env
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
BOOTSTRAP_OWNER_ID=123456789

AWG_WEB_BASE_URL=http://127.0.0.1:8080
AWG_WEB_API_TOKEN=your_amneziawg_web_api_token

BOT_DB=/root/TGbots/amneziawg-telegram-bot/data/bot.sqlite3

REQUEST_TIMEOUT=20
PEERS_PAGE_SIZE=8
EVENTS_LIMIT=20
TRAFFIC_CHECK_INTERVAL=300
```

## Переменные окружения

### `BOT_TOKEN`

Токен Telegram-бота от BotFather.

Обязательно.

```env
BOT_TOKEN=123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

### `BOOTSTRAP_OWNER_ID`

Telegram ID первого владельца бота.

Обязательно.

```env
BOOTSTRAP_OWNER_ID=123456789
```

При первом запуске этот пользователь автоматически добавляется в таблицу администраторов с ролью `owner`.

### `AWG_WEB_BASE_URL`

Базовый URL `amneziawg-web`.

По умолчанию:

```env
AWG_WEB_BASE_URL=http://127.0.0.1:8080
```

Если `amneziawg-web` работает на другом адресе или порту, укажите правильный URL.

### `AWG_WEB_API_TOKEN`

API-токен `amneziawg-web`.

Обязательно.

```env
AWG_WEB_API_TOKEN=your_amneziawg_web_api_token
```

Бот передаёт его в HTTP-заголовке:

```text
Authorization: Bearer <token>
```

### `BOT_DB`

Путь к SQLite-базе бота.

По умолчанию:

```env
BOT_DB=/root/TGbots/amneziawg-telegram-bot/data/bot.sqlite3
```

### `REQUEST_TIMEOUT`

Таймаут HTTP-запросов к `amneziawg-web` в секундах.

По умолчанию:

```env
REQUEST_TIMEOUT=20
```

### `PEERS_PAGE_SIZE`

Количество клиентов на одной странице списка.

По умолчанию:

```env
PEERS_PAGE_SIZE=8
```

### `EVENTS_LIMIT`

Количество событий `amneziawg-web`, показываемых в разделе Events.

По умолчанию:

```env
EVENTS_LIMIT=20
```

### `TRAFFIC_CHECK_INTERVAL`

Интервал фоновой проверки лимитов трафика в секундах.

По умолчанию:

```env
TRAFFIC_CHECK_INTERVAL=300
```

Минимальное фактическое значение в коде — 30 секунд.

## Проверка API `amneziawg-web`

Перед запуском бота проверьте, что API доступен.

Пример:

```bash
curl -H "Authorization: Bearer your_amneziawg_web_api_token" \
     http://127.0.0.1:8080/api/health
```

Если всё настроено правильно, должен вернуться ответ от API.

Если API недоступен, бот не сможет управлять клиентами.

## Первый запуск

Перейдите в директорию проекта:

```bash
cd /root/TGbots/amneziawg-telegram-bot
```

Активируйте виртуальное окружение:

```bash
source .venv/bin/activate
```

Запустите бота:

```bash
python bot.py
```

В логах должно появиться примерно:

```text
Starting bot
AWG_WEB_BASE_URL=http://127.0.0.1:8080/
BOT_DB=/root/TGbots/amneziawg-telegram-bot/data/bot.sqlite3
```

Откройте Telegram и отправьте боту:

```text
/start
```

Если ваш Telegram ID совпадает с `BOOTSTRAP_OWNER_ID`, появится главное меню.

## Запуск через systemd

### 1. Создайте unit-файл

```bash
nano /etc/systemd/system/amneziawg-telegram-bot.service
```

Содержимое:

```ini
[Unit]
Description=AmneziaWG Telegram Admin Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/root/TGbots/amneziawg-telegram-bot
EnvironmentFile=/root/TGbots/amneziawg-telegram-bot/.env
ExecStart=/root/TGbots/amneziawg-telegram-bot/.venv/bin/python /root/TGbots/amneziawg-telegram-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 2. Перечитайте конфигурацию systemd

```bash
systemctl daemon-reload
```

### 3. Включите автозапуск

```bash
systemctl enable amneziawg-telegram-bot
```

### 4. Запустите сервис

```bash
systemctl start amneziawg-telegram-bot
```

### 5. Проверьте статус

```bash
systemctl status amneziawg-telegram-bot
```

### 6. Посмотреть логи

```bash
journalctl -u amneziawg-telegram-bot -f
```

## Права доступа

При первом запуске бот создаёт SQLite-базу и добавляет пользователя из `BOOTSTRAP_OWNER_ID` как `owner`.

Роли:

- `owner`;
- `admin`.

### Owner может:

- пользоваться всеми функциями;
- добавлять администраторов;
- добавлять других owner;
- удалять администраторов;
- сбрасывать общую статистику бота.

### Admin может:

- просматривать клиентов;
- создавать клиентов;
- скачивать конфиги;
- получать QR;
- редактировать клиентов;
- включать/отключать клиентов;
- удалять клиентов;
- управлять DNS и `I1-I5`;
- задавать лимиты трафика.

## База данных

Бот использует SQLite.

Основные таблицы:

```text
bot_admins
bot_settings
dns_presets
bot_audit_log
peer_daily_usage
server_daily_usage
bot_created_peers
pending_created_clients
peer_traffic_limits
```

База создаётся автоматически при первом запуске.

По умолчанию:

```text
/root/TGbots/amneziawg-telegram-bot/data/bot.sqlite3
```

## DNS-пресеты

По умолчанию создаётся DNS-пресет:

```text
cloudflare
```

С серверами:

```text
1.1.1.1
1.0.0.1
8.8.8.8
9.9.9.9
```

Активный DNS-пресет используется при отправке `.conf` и QR-кода.

В исходном конфиге строка:

```text
DNS =
```

заменяется на DNS из активного пресета.

## Параметры I1-I5

Бот позволяет задавать параметры:

```text
I1
I2
I3
I4
I5
```

Особенности:

- если `I1` не задан, `I2-I5` недоступны;
- если очистить `I1`, `I2-I5` тоже очищаются;
- параметры добавляются в отправляемую копию конфига после строки `H4`;
- если строка `H4` не найдена, бот покажет предупреждение.

## Обработка IPv6 в конфиге

В меню:

```text
🧩 Конфиг
```

можно выбрать:

- удалять IPv6 из `Address`;
- оставлять IPv6 в `Address`.

По умолчанию IPv6 удаляется.

Это влияет только на копию конфига, которую бот отправляет в Telegram.

## Создание клиента

В главном меню нажмите:

```text
➕ Создать
```

Введите имя клиента.

Разрешены:

```text
A-Z
a-z
0-9
_
-
```

Максимум 15 символов.

После создания бот:

1. вызывает API `amneziawg-web`;
2. пытается найти созданный peer;
3. скачивает конфиг;
4. применяет изменения к копии конфига;
5. отправляет `.conf`;
6. отправляет QR-код.

## Карточка клиента

В карточке клиента отображаются:

- ID;
- Public key;
- статус подключения;
- статус identity;
- disabled;
- наличие конфига;
- Last handshake;
- Endpoint;
- Allowed IPs;
- RX/TX total;
- config/friendly/display name/comment;
- лимит;
- дата и время обновления карточки.

`Last handshake` показывается в формате:

```text
ГГГГ/ММ/ДД ЧЧ:мм:сс
```

с учётом локального часового пояса машины, на которой запущен бот.

Внизу карточки есть:

```text
Обновлено: ГГГГ/ММ/ДД ЧЧ:мм:сс
```

Кнопка:

```text
🔄 Обновить
```

повторно запрашивает данные клиента из API.

## Лимиты трафика

В карточке клиента нажмите:

```text
🚦 Лимит
```

Доступные периоды:

- `never` — лимит по общему RX+TX;
- `day` — лимит на день;
- `week` — лимит на неделю;
- `month` — лимит на месяц.

Чтобы задать лимит:

1. нажмите кнопку `Задать: ...`;
2. введите значение.

Примеры значений:

```text
10GB
500MB
10737418240
```

Поддерживаются единицы:

```text
B
KB
MB
GB
TB
PB
Б
КБ
МБ
ГБ
ТБ
ПБ
```

При превышении лимита бот отключает клиента через API.

Если период не `never`, то в новом периоде бот автоматически включает клиента обратно, если сам отключил его по лимиту.

Фоновая проверка лимитов выполняется с интервалом:

```env
TRAFFIC_CHECK_INTERVAL=300
```

## Статистика

### Серверная статистика

В меню:

```text
📊 Сервер
```

показывается:

- день;
- неделя;
- месяц;
- год SQLite.

Годовая статистика считается по данным, которые накопил сам бот в SQLite.

### Статистика клиента

В карточке клиента нажмите:

```text
📈 Трафик
```

Показывается:

- день;
- неделя;
- месяц;
- год SQLite.

Для клиента доступна кнопка:

```text
🧹 Сбросить статистику бота
```

Она очищает только данные клиента из таблицы:

```text
peer_daily_usage
```

Статистика `amneziawg-web` не сбрасывается.

## Аудит

В меню:

```text
📜 Лог бота
```

отображаются последние действия:

- создание клиента;
- удаление клиента;
- скачивание конфига;
- получение QR;
- изменение DNS;
- изменение `I1-I5`;
- изменение лимитов;
- добавление/удаление администраторов;
- отказы в доступе.

## Events amneziawg-web

В меню:

```text
📋 Events web
```

бот показывает события из API:

```text
/api/events
```

Количество событий задаётся переменной:

```env
EVENTS_LIMIT=20
```

## Проверка часового пояса

Бот форматирует локальное время через timezone процесса Python.

Проверьте timezone машины:

```bash
timedatectl
```

Пример корректного вывода для UTC+3:

```text
Time zone: Europe/Moscow
```

Если бот запущен в Docker или другом изолированном окружении, timezone внутри окружения может отличаться от timezone хоста.

## Обновление бота

1. Остановите сервис:

```bash
systemctl stop amneziawg-telegram-bot
```

2. Обновите `bot.py`.

3. При необходимости обновите зависимости:

```bash
cd /root/TGbots/amneziawg-telegram-bot
source .venv/bin/activate
pip install -r requirements.txt
```

4. Запустите сервис:

```bash
systemctl start amneziawg-telegram-bot
```

5. Проверьте логи:

```bash
journalctl -u amneziawg-telegram-bot -f
```

## Резервное копирование

Рекомендуется регулярно сохранять:

```text
/root/TGbots/amneziawg-telegram-bot/.env
/root/TGbots/amneziawg-telegram-bot/data/bot.sqlite3
/root/TGbots/amneziawg-telegram-bot/bot.py
```

Пример:

```bash
tar -czf amneziawg-telegram-bot-backup.tar.gz \
  /root/TGbots/amneziawg-telegram-bot/.env \
  /root/TGbots/amneziawg-telegram-bot/data/bot.sqlite3 \
  /root/TGbots/amneziawg-telegram-bot/bot.py
```

## Возможные ошибки

### Доступ запрещён

Причина:

- ваш Telegram ID не добавлен в `bot_admins`;
- `BOOTSTRAP_OWNER_ID` указан неверно;
- база уже была создана с другим owner.

Решение:

- проверьте свой Telegram ID;
- проверьте `.env`;
- при первичной установке можно удалить SQLite-базу и запустить бота заново:

```bash
systemctl stop amneziawg-telegram-bot
rm /root/TGbots/amneziawg-telegram-bot/data/bot.sqlite3
systemctl start amneziawg-telegram-bot
```

Внимание: удаление базы удалит настройки бота, DNS-пресеты, админов, аудит и SQLite-статистику.

### Ошибка соединения с amneziawg-web

Проверьте:

```bash
curl -H "Authorization: Bearer your_amneziawg_web_api_token" \
     http://127.0.0.1:8080/api/health
```

Также проверьте:

```env
AWG_WEB_BASE_URL
AWG_WEB_API_TOKEN
```

### Бот не запускается

Проверьте статус:

```bash
systemctl status amneziawg-telegram-bot
```

Проверьте логи:

```bash
journalctl -u amneziawg-telegram-bot -n 100 --no-pager
```

Частые причины:

- не задан `BOT_TOKEN`;
- неверный `BOOTSTRAP_OWNER_ID`;
- не задан `AWG_WEB_API_TOKEN`;
- не установлены зависимости;
- ошибка синтаксиса в `bot.py`;
- нет прав на директорию `data`.

### Не создаётся SQLite-база

Проверьте права:

```bash
ls -ld /root/TGbots/amneziawg-telegram-bot/data
```

Создайте директорию:

```bash
mkdir -p /root/TGbots/amneziawg-telegram-bot/data
```

## Минимальная проверка после установки

1. Запустить сервис:

```bash
systemctl start amneziawg-telegram-bot
```

2. Проверить статус:

```bash
systemctl status amneziawg-telegram-bot
```

3. Открыть Telegram.

4. Отправить боту:

```text
/start
```

5. Проверить:

```text
🩺 Health
```

6. Проверить:

```text
👥 Клиенты
```

Если health и список клиентов открываются, базовая установка выполнена корректно.