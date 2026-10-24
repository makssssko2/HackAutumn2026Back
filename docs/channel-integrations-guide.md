# Интеграции VK Teams, Mattermost и Bitrix24: подробный разбор

Этот документ описывает фактически реализованную backend-фичу интеграций Invoice Keeper с VK Teams, Mattermost и Bitrix24, её жизненный цикл и способ подключения frontend.

## 1. Что делает фича

Администратор может подключить бота VK Teams или Mattermost, привязать к нему чат и добавить этот чат в аудиторию рассылки. После этого worker Invoice Keeper отправляет туда:

- новый счёт;
- напоминание о неоплаченном счёте;
- изменение статуса счёта;
- демонстрационный HTML-чек после оплаты.

Участник чата может нажать кнопку оплаты. Кнопка не передаёт в мессенджер права администратора и не содержит идентификатор счёта в открытом виде. Backend выдаёт непрозрачный одноразовый action-токен, проверяет его и передаёт оплату в общий доменный сервис `confirm_occurrence`.

Таким образом, web/API, Telegram, VK Teams и Mattermost используют одни и те же правила:

- проверка текущей revision счёта;
- запрет оплаты отменённого счёта;
- запрет будущей даты оплаты;
- защита от двух одновременных подтверждений;
- создание и аннулирование демонстрационных чеков;
- аудит операции;
- постановка события обновления сообщений.

Деньги реально не списываются. Оплата в текущем проекте демонстрационная.

## 2. Архитектура

### 2.1. Connection и Channel

В системе разделены два понятия.

`IntegrationConnection` описывает один сервер и одного бота:

```text
provider       = vk_teams | mattermost | bitrix24
name           = отображаемое имя подключения
base_url       = URL Bot API или Mattermost server
credentials    = зашифрованный токен
external_bot_id
capabilities
setup_status
health_status
generation
server_revision
```

`Channel` описывает конкретный чат-получатель:

```text
kind           = vk_teams | mattermost | bitrix24
connection_id  = IntegrationConnection.id
remote_chat_id = ID чата у провайдера
active         = включён ли получатель
```

Одно подключение может обслуживать несколько чатов. Один и тот же `chat_id` на разных серверах не считается одним и тем же чатом: контекст всегда определяется через `connection_id`.

Старые каналы `telegram`, `email`, `webhook` и `inapp` продолжают работать без `IntegrationConnection`.

### 2.2. Исходящая цепочка

```text
Создание/напоминание/изменение счёта
        |
        v
domain.services.queue_event()
        |
        v
Delivery(occurrence_id, channel_id, event)
        |
        v
worker -> notifications.process_deliveries()
        |
        v
send_delivery()
        |
        +--> MattermostAdapter
        |
        +--> VkTeamsAdapter
        |
        v
remote_message_id сохраняется в Delivery
```

Очередь `Delivery` остаётся общей. Поэтому новая интеграция не создаёт отдельную систему рассылок и использует существующие retry, uncertain и release-правила.

### 2.3. Входящая цепочка

Mattermost отправляет callback на публичный endpoint приложения:

```text
Mattermost button
        |
        v
POST /api/v1/integrations/mattermost/{connection_id}/actions
        |
        v
parse_callback()
        |
        v
BotAction validation
        |
        v
confirm_occurrence(..., demo=True)
```

VK Teams использует polling:

```text
worker tick
        |
        v
events/get(lastEventId=server_revision)
        |
        v
IntegrationInbox deduplication
        |
        v
parse callback -> BotAction validation -> confirm_occurrence
        |
        v
server_revision обновляется после сохранения события
```

## 3. Безопасность

### 3.1. Токены подключения

Токены ботов не хранятся в `Channel.config` и не возвращаются в GET-ответах.

Перед сохранением credentials шифруются Fernet-ключом из серверной переменной:

```dotenv
INTEGRATIONS_ENCRYPTION_KEY=...
```

Ключ нужно сгенерировать один раз и хранить в secrets/environment manager. Он не должен попадать в frontend, git, audit или логи.

Пример генерации:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Если ключ не задан, создание или обновление подключения завершается ошибкой. Plaintext fallback не используется.

### 3.2. Action-токены

При отправке неоплаченного счёта backend создаёт `BotAction`:

```text
token
occurrence_id
delivery_id
channel_id
connection_id
provider
chat_id
message_id
revision
mode
expires_at
consumed_at
```

В payload кнопки попадает только случайный токен. При callback backend проверяет:

1. токен существует;
2. токен относится к этому `connection_id` и provider;
3. callback пришёл из ожидаемого чата;
4. callback относится к ожидаемому сообщению, если message ID передан;
5. канал активен;
6. токен не истёк и не использован;
7. revision счёта не изменилась.

HTTP callback сам по себе не считается доказательством личности. Доверие строится на провайдерском callback-контексте и непредсказуемом action-токене, выданном конкретному сообщению.

### 3.3. Inbound deduplication

`IntegrationInbox` имеет уникальность:

```text
(connection_id, external_event_id)
```

Повторный event от VK Teams не должен повторно оплачивать счёт или создавать второй чек.

## 4. Поддержка VK Teams

### 4.1. Настройка

Для VK Teams создаётся подключение:

```json
{
  "provider": "vk_teams",
  "name": "VK Teams компании",
  "base_url": "https://myteam.mail.ru/bot/v1",
  "credentials": {
    "token": "VK_TEAMS_BOT_TOKEN"
  }
}
```

Для корпоративного окружения `base_url` может быть другим. Нельзя без проверки подставлять облачный URL в on-premise окружение.

### 4.2. Используемые методы

Adapter использует официальный Bot API:

- `self/get` — проверка токена и получение bot ID;
- `messages/sendText` — отправка счёта;
- `messages/editText` — обновление счёта;
- `messages/sendFile` — отправка HTML-чека;
- `messages/answerCallbackQuery` — подтверждение обработки кнопки;
- `events/get` — long polling событий.

Токен передаётся провайдеру как query parameter `token`. Он не должен попадать в application logs.

### 4.3. Кнопки

VK Teams получает inline keyboard с `callbackData`, равным action-токену. Backend не использует открытые значения вроде `pay:{occurrence_id}`.

### 4.4. Polling cursor

Последний обработанный event ID хранится в `IntegrationConnection.server_revision`. Worker передаёт его в следующий `events/get` как `lastEventId`.

Перед продвижением cursor событие сохраняется в `IntegrationInbox`. Если event уже существует, он пропускается.

## 5. Поддержка Mattermost

### 5.1. Настройка

Для Mattermost нужен bot account с access token. Incoming webhook не используется как основной режим, потому что он не даёт полноценного управления постами, callback и файлами.

```json
{
  "provider": "mattermost",
  "name": "Mattermost компании",
  "base_url": "https://chat.company.example",
  "credentials": {
    "access_token": "MATTERMOST_BOT_ACCESS_TOKEN"
  }
}
```

Токен должен быть access token bot account, а не Token ID.

### 5.2. Используемые методы

- `GET /api/v4/users/me` — проверка токена;
- `POST /api/v4/posts` — отправка сообщения;
- `PUT /api/v4/posts/{post_id}/patch` — редактирование сообщения;
- `POST /api/v4/files` — загрузка чека.

Авторизация выполняется через:

```http
Authorization: Bearer MATTERMOST_BOT_ACCESS_TOKEN
```

### 5.3. Callback URL

Mattermost должен иметь возможность обратиться к:

```text
POST {PUBLIC_APP_URL}/api/v1/integrations/mattermost/{connection_id}/actions
```

В production `PUBLIC_APP_URL` должен быть публичным HTTPS URL. Нельзя строить callback URL из непроверенного HTTP Host-заголовка.

Для локального Mattermost callback через `localhost` обычно не сработает: Mattermost должен видеть адрес приложения по сети.

### 5.4. Interactive blocks

В `props` Mattermost передаются blocks и registry действий. В context кнопки находится action-токен. Backend не доверяет только `user_id` или `channel_id` из JSON, а сверяет их с сохранённым `BotAction`.

Если сервер Mattermost не поддерживает interactive blocks или не может обратиться к callback URL, подключение нельзя считать полностью готовым, даже если обычное сообщение отправляется.

## 5.4. Поддержка Bitrix24 Chatbots 2.0

Bitrix24 использует только `imbot.v2` в режиме `fetch`: CRM, Open Channels, legacy `imbot`, OAuth и публичный callback endpoint не используются.

Создайте connection с portal origin в `base_url` и полным incoming webhook URL со scope `imbot` в write-only credentials:

```json
{
  "provider": "bitrix24",
  "name": "Bitrix24 компании",
  "base_url": "https://company.bitrix24.ru",
  "credentials": {
    "webhook_url": "https://company.bitrix24.ru/rest/1/webhook-code"
  }
}
```

Затем admin вызывает `POST /api/v1/integrations/connections/{connection_id}/setup`. Backend создаёт стабильный bot code, генерирует и шифрует bot token длиной не более 40 символов, регистрирует bot `eventMode: "fetch"` и hidden command `confirm`. Ответы API не возвращают webhook или bot token.

Отдельный runner `python -m app.bitrix24_runner` вызывает `imbot.v2.Event.get` без `offset` при первом запросе и с сохранённым `nextOffset` далее, поэтому fetch wait не задерживает обычный delivery worker. В Docker он запускается сервисом `bitrix-events`. Runner сохраняет `(connection_id, eventId)` и результат обработки до продвижения offset; между страницами `hasMore` выдерживает две секунды. Для `ONIMBOTV2COMMANDADD` принимается только keyboard command `confirm`; `COMMAND_PARAMS` содержит JSON только с opaque action token и mode. Bot ID, dialog ID, message ID, provider, connection и invoice revision проверяются перед общим `confirm_occurrence`.

Для `bitrix24` channel `remote_chat_id` должен быть Bitrix dialog ID, например `chat123` для группового чата. Отправка использует `Chat.Message.send`/`Chat.Message.update`; после оплаты worker загружает HTML-чек через `File.upload` (лимит Bitrix24 100 MB, приложение генерирует существенно меньший файл).

## 6. Backend API для frontend

Все административные endpoints требуют сессию пользователя с ролью `admin`.

### 6.1. Список провайдеров

```http
GET /api/v1/integrations/providers
```

Пример ответа:

```json
[
  {
    "provider": "vk_teams",
    "label": "VK Teams",
    "credentials": ["token"],
    "capabilities": ["native_actions", "message_edit", "file_upload", "polling"]
  },
  {
    "provider": "mattermost",
    "label": "Mattermost",
    "credentials": ["access_token"],
    "capabilities": ["native_actions", "message_edit", "file_upload", "callback"]
  }
]
```

Frontend должен строить выбор сервиса по `provider`, а не по display label.

### 6.2. Создание подключения

```http
POST /api/v1/integrations/connections
Content-Type: application/json
```

Тело для Mattermost:

```json
{
  "provider": "mattermost",
  "name": "Основной Mattermost",
  "base_url": "https://chat.company.example",
  "credentials": {
    "access_token": "secret-token"
  }
}
```

Тело для VK Teams:

```json
{
  "provider": "vk_teams",
  "name": "Основной VK Teams",
  "base_url": "https://myteam.mail.ru/bot/v1",
  "credentials": {
    "token": "secret-token"
  }
}
```

Ответ не содержит исходные credentials:

```json
{
  "id": "connection-id",
  "provider": "mattermost",
  "name": "Основной Mattermost",
  "base_url": "https://chat.company.example",
  "active": true,
  "setup_status": "configured",
  "health_status": "not_checked",
  "has_credentials": true,
  "external_bot_id": null,
  "capabilities": {},
  "last_checked_at": null,
  "last_event_at": null,
  "last_error_code": null,
  "generation": 1
}
```

### 6.3. Проверка подключения

```http
POST /api/v1/integrations/connections/{connection_id}/validate
```

При успехе backend вызывает `self/get` VK Teams или `/api/v4/users/me` Mattermost, сохраняет bot ID и capabilities, после чего возвращает `health_status: "healthy"` и `setup_status: "ready"`.

При ошибке frontend должен показать безопасное сообщение по `last_error_code`. Raw response провайдера показывать не нужно.

### 6.4. Изменение подключения

```http
PATCH /api/v1/integrations/connections/{connection_id}
```

Можно передать только изменённые поля:

```json
{
  "name": "Новое имя",
  "active": false
}
```

Credentials отправляются только при явной ротации:

```json
{
  "credentials": {
    "access_token": "new-token"
  }
}
```

Frontend не должен отправлять маскированное значение вроде `********` как новый токен.

### 6.5. Создание канала

```http
POST /api/v1/channels
```

Пример для Mattermost:

```json
{
  "name": "Финансовый канал Mattermost",
  "kind": "mattermost",
  "connection_id": "connection-id",
  "remote_chat_id": "channel-id",
  "config": {},
  "active": true
}
```

Пример для VK Teams:

```json
{
  "name": "Группа VK Teams",
  "kind": "vk_teams",
  "connection_id": "connection-id",
  "remote_chat_id": "chat-id",
  "config": {},
  "active": true
}
```

Backend проверяет, что:

- `connection_id` существует;
- provider connection совпадает с `kind` канала;
- connection активен;
- `remote_chat_id` задан.

Нельзя применять к VK Teams/Mattermost Telegram-правило «chat ID должен быть числом».

## 7. Как встроить во frontend

### 7.1. Типы

Рекомендуемые TypeScript-типы:

```ts
export type IntegrationProvider = 'vk_teams' | 'mattermost'

export type ConnectionHealth = 'not_checked' | 'healthy' | 'error'

export interface IntegrationProviderInfo {
  provider: IntegrationProvider
  label: string
  credentials: string[]
  capabilities: string[]
}

export interface IntegrationConnection {
  id: string
  provider: IntegrationProvider
  name: string
  base_url: string
  active: boolean
  setup_status: string
  health_status: ConnectionHealth
  has_credentials: boolean
  external_bot_id: string | null
  capabilities: Record<string, unknown>
  last_checked_at: string | null
  last_event_at: string | null
  last_error_code: string | null
  generation: number
}

export interface ChatChannel {
  id: string
  name: string
  kind: IntegrationProvider
  connection_id: string
  remote_chat_id: string
  active: boolean
}
```

### 7.2. Состояние мастера

Frontend может реализовать один modal/wizard с шагами:

```text
provider
  -> credentials
  -> validate
  -> destination/chat ID
  -> create channel
  -> test delivery
  -> ready
```

Рекомендуемое состояние:

```ts
type WizardStep =
  | 'provider'
  | 'credentials'
  | 'validating'
  | 'destination'
  | 'testing'
  | 'ready'
  | 'error'
```

В state не хранить секрет дольше, чем нужно форме. Не класть credentials в localStorage, sessionStorage, query string, analytics events или Vue devtools state, если это можно избежать.

### 7.3. Пример API-клиента

```ts
async function createConnection(input: {
  provider: IntegrationProvider
  name: string
  base_url: string
  credentials: Record<string, string>
}) {
  const response = await fetch('/api/v1/integrations/connections', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(input),
  })

  if (!response.ok) {
    throw await readApiError(response)
  }
  return await response.json() as IntegrationConnection
}

async function validateConnection(connectionId: string) {
  const response = await fetch(
    `/api/v1/integrations/connections/${connectionId}/validate`,
    { method: 'POST', credentials: 'include' },
  )

  if (!response.ok) {
    throw await readApiError(response)
  }
  return await response.json() as IntegrationConnection
}

async function createChatChannel(input: {
  name: string
  kind: IntegrationProvider
  connection_id: string
  remote_chat_id: string
}) {
  const response = await fetch('/api/v1/channels', {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...input, config: {}, active: true }),
  })

  if (!response.ok) {
    throw await readApiError(response)
  }
  return await response.json() as ChatChannel
}
```

`readApiError` должен превращать 401, 403, 409, 422 и 503 в понятное состояние UI, не отображая credentials или raw provider response.

### 7.4. Обязательная последовательность

1. Загрузить `/integrations/providers`.
2. Пользователь выбирает VK Teams или Mattermost.
3. Показать только поля выбранного provider.
4. Создать connection с credentials.
5. Вызвать `validate`.
6. Только после успешной проверки предложить создать channel.
7. Сохранить `connection_id` и `remote_chat_id` в channel.
8. Обновить список каналов и доступных аудиторий.
9. Добавить канал в группу рассылки или выбрать его при создании счёта.

Не показывать `connected`, если connection создан, но `health_status` равен `not_checked` или `error`.

## 8. Обработка ошибок во frontend

| HTTP/код | UI-поведение |
|---|---|
| 401 | Сессия истекла, перейти на login |
| 403 | Показать, что операция доступна только администратору |
| 409 | Обновить connection/channel и показать конфликт текущего состояния |
| 422 `credentials_invalid` | Проверить token/access token и URL |
| 422 `provider_rejected` | Показать, что провайдер отклонил запрос |
| 422 `invalid_callback` | Проверить callback payload или настройки провайдера |
| 503 | Показать, что серверная encryption key не настроена |
| delivery `retry` | Показать повторную попытку |
| delivery `uncertain` | Не запускать слепной retry; попросить проверить чат |
| `connection_unavailable` | Отключить channel до восстановления connection |

При оплате через чат frontend Invoice Keeper не получает callback напрямую. Frontend должен обновить данные счёта обычным API-запросом после изменения страницы или использовать существующий механизм обновления данных.

Важно: ошибка обновления сообщения в Mattermost/VK Teams не должна визуально откатывать оплату. Сначала сохраняется бизнес-статус, затем worker пытается синхронизировать внешние сообщения.

## 9. Аудитории и счета

Новые каналы используют существующую модель `AudienceGroup.channel_ids`. Отдельные списки `mattermost_channel_ids` или `vk_teams_chat_ids` не нужны.

Перед созданием счета frontend должен:

- показывать новые каналы в preview аудитории;
- различать `active` channel и реально проверенное connection;
- предупреждать о неготовом connection;
- не удалять выбранный channel молча;
- одинаково поддерживать recurring и one-time payment.

После создания счета дальнейшая доставка выполняется backend worker. Frontend не должен сам отправлять сообщения в VK Teams или Mattermost.

## 10. Тестирование frontend

Нужны проверки:

- выбор VK Teams показывает token и Bot API URL;
- выбор Mattermost показывает server URL и access token;
- смена provider очищает секрет предыдущего provider;
- секрет не сохраняется в localStorage/sessionStorage;
- успешное создание connection не показывает credentials в карточке;
- validate меняет health status;
- channel получает правильные `connection_id` и `remote_chat_id`;
- нечисловой VK Teams/Mattermost chat ID принимается;
- невалидный provider/channel conflict отображается понятно;
- новый channel появляется в audience preview;
- `uncertain` не отображается как обычный `failed`;
- обычный `user` не видит admin wizard и не может вызвать admin API.

Для frontend mock API достаточно подменить:

```text
GET  /api/v1/integrations/providers
GET  /api/v1/integrations/connections
POST /api/v1/integrations/connections
POST /api/v1/integrations/connections/{id}/validate
POST /api/v1/channels
```

Live Mattermost/VK Teams не требуется для unit/component-тестов frontend.

## 11. Production checklist

### Backend

- задан стабильный `INTEGRATIONS_ENCRYPTION_KEY`;
- задан корректный `PUBLIC_APP_URL` с HTTPS;
- применена миграция `0007`;
- worker запущен;
- для VK Teams разрешён исходящий доступ к Bot API;
- для Mattermost сервер может обратиться к callback URL;
- bot account добавлен в нужный чат;
- credentials не попали в логи и git;
- создан тестовый channel и проверена отправка.

### Frontend

- подключены новые типы channel;
- добавлены icons/labels VK Teams и Mattermost;
- wizard не показывает фиктивный успех;
- credentials отправляются только по HTTPS и только на backend;
- состояние `not_checked`, `healthy`, `error` различается;
- аудитории и журнал уведомлений отображают новые каналы;
- обычный пользователь не получает доступ к connection settings.

### Live smoke

Для настоящей проверки необходимо отдельно выполнить:

1. создать connection;
2. вызвать validate;
3. создать channel;
4. отправить тестовый счёт;
5. нажать кнопку в чате;
6. проверить изменение occurrence и создание чека;
7. проверить редактирование сообщения;
8. перезапустить worker и убедиться, что событие не обрабатывается повторно.

До появления реальных токенов и серверов результат следует называть «проверено по mock-контрактам», а не «проверено в production-инсталляции».

## 12. Фактический статус реализации

Реализовано в backend:

- модели connection/channel/inbox/action;
- Fernet encryption credentials;
- admin API connections;
- Mattermost adapter;
- VK Teams adapter;
- delivery worker dispatch;
- Mattermost callback;
- VK Teams polling;
- action-token validation;
- общий `confirm_occurrence`;
- отправка чека;
- mock contract tests;
- миграция `0007_chat_integrations.py`.

Отдельно для следующего этапа frontend нужно реализовать wizard и обновить existing channel settings, groups, payment audience preview и notifications journal. Backend уже предоставляет базовые endpoint-ы, перечисленные в этом документе.
