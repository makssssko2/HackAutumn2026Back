# Bitrix24 Chatbots 2.0: интеграция Invoice Keeper

Документ описывает реализованную backend-интеграцию Invoice Keeper с корпоративным мессенджером Bitrix24 через официальный API Chatbots 2.0 (`imbot.v2`).

Интеграция не использует Bitrix CRM, Open Channels, OAuth, legacy `imbot` и `im.message.*`. Сценарий предназначен для сообщений бота в чатах Bitrix24 и подтверждения демонстрационной оплаты кнопкой.

## 1. Возможности

После подключения Bitrix24 Invoice Keeper может:

- отправлять новые счета и напоминания в групповой чат;
- редактировать сообщение после изменения статуса счета;
- показывать кнопку подтверждения оплаты;
- принимать нажатие кнопки через `ONIMBOTV2COMMANDADD`;
- подтверждать демонстрационную оплату через общий `confirm_occurrence`;
- создавать демонстрационный чек;
- отправлять HTML-чек нативным файлом в Bitrix24;
- безопасно повторно обрабатывать события после перезапуска;
- не отправлять исторические счета каналу, подключённому позже release периода.

Денежные средства не списываются. Оплата в Invoice Keeper демонстрационная.

## 2. Почему используется Chatbots 2.0

Bitrix24 имеет несколько разных API-сценариев. В этой интеграции выбран именно `imbot.v2`:

- бот работает внутри чатов Bitrix24;
- кнопка использует зарегистрированную команду;
- событие кнопки приходит как `ONIMBOTV2COMMANDADD`;
- входящие события читаются через `imbot.v2.Event.get` в режиме `fetch`;
- не нужен публичный callback endpoint Bitrix24;
- cursor событий хранится на backend.

Не используется простой `ACTION`-button: он не является надёжным источником события подтверждения оплаты. Для оплаты используется `COMMAND` + `COMMAND_PARAMS`.

## 3. Модель данных

### IntegrationConnection

Подключение Bitrix24 хранит:

```text
provider          = bitrix24
name              = отображаемое имя
base_url          = origin портала
credentials       = зашифрованные webhook_url и bot_token
external_bot_id   = botId Bitrix24
public_config     = bot_code, bot_name, command, public metadata
setup_status      = configured | ready
health_status     = not_checked | healthy | error
server_revision   = nextOffset Event.get
capabilities      = подтвержденные возможности
generation        = версия подключения
```

`webhook_url` и `bot_token` не возвращаются API, не записываются в audit и не попадают в обычный channel config.

### Channel

Bitrix24 получатель хранится как обычный `Channel`:

```text
kind           = bitrix24
connection_id  = IntegrationConnection.id
remote_chat_id = Bitrix24 dialogId
active         = true/false
```

Для группового чата `remote_chat_id` обычно имеет вид:

```text
chat123
```

Нельзя применять Telegram-валидацию «ID должен быть числом».

### BotAction

Для каждой кнопки счета создаётся непрозрачный action token:

```text
token
occurrence_id
delivery_id
channel_id
connection_id
provider
message_id
chat_id
revision
expires_at
consumed_at
```

Bitrix24 получает только token внутри `COMMAND_PARAMS`.

## 4. Настройка Bitrix24

### Шаг 1. Создать incoming webhook

В Bitrix24 создайте входящий webhook с доступом `imbot`.

Полный webhook URL является секретом. Его нельзя отправлять во frontend logs, query parameters или обычное поле публичной конфигурации.

### Шаг 2. Создать connection

```http
POST /api/v1/integrations/connections
Content-Type: application/json
```

```json
{
  "provider": "bitrix24",
  "name": "Bitrix24 компании",
  "base_url": "https://company.bitrix24.ru",
  "credentials": {
    "webhook_url": "https://company.bitrix24.ru/rest/1/webhook-token"
  }
}
```

`base_url` нужен для отображения и проверки окружения. Реальный API-вызов выполняется через полный encrypted `webhook_url`.

### Шаг 3. Проверить доступ

```http
POST /api/v1/integrations/connections/{connection_id}/validate
```

Backend вызывает:

imbot.v2.Revision.get
```

Этот метод не требует `botId` и `botToken`. Проверка не регистрирует бота и не отправляет сообщений.

Успешный ответ переводит connection в `health_status: healthy`.

### Шаг 4. Выполнить setup

```http
POST /api/v1/integrations/connections/{connection_id}/setup
```

Backend:

1. создаёт стабильный code бота на основе connection ID;
2. генерирует `botToken` длиной не более 40 символов;
3. вызывает `imbot.v2.Bot.register` в режиме `eventMode: fetch`;
4. сохраняет `botId` и token в encrypted credentials;
5. регистрирует скрытую команду `confirm`;
6. сохраняет безопасный setup status.

Регистрация использует вложенное поле `fields`:

```json
{
  "fields": {
    "code": "invoice_keeper_connection_code",
    "botToken": "generated-secret",
    "properties": {
      "name": "Bitrix24 компании"
    },
    "type": "bot",
    "eventMode": "fetch"
  }
}
```

Повторный вызов setup использует тот же code и не создаёт новый бот.

Команда регистрируется в текущем формате Bitrix24:

```json
{
  "botId": "123",
  "botToken": "generated-secret",
  "fields": {
    "command": "confirm",
    "title": {
      "en": "Confirm payment",
      "ru": "Подтвердить оплату"
    },
    "hidden": true,
    "common": false
  }
}
```

### Шаг 5. Добавить бота в чат

Добавьте созданного бота в нужный групповой чат Bitrix24 и определите его `dialogId`.

После этого создайте канал:

```http
POST /api/v1/channels
```

```json
{
  "name": "Финансовый чат Bitrix24",
  "kind": "bitrix24",
  "connection_id": "connection-id",
  "remote_chat_id": "chat123",
  "config": {},
  "active": true
}
```

Backend проверяет, что connection существует, относится к Bitrix24 и включён.

## 5. Исходящая доставка счета

Жизненный цикл доставки не меняется:

```text
Occurrence event
    -> Delivery
    -> worker
    -> notifications.send_delivery
    -> Bitrix24Adapter
    -> Chat.Message.send
    -> Delivery.remote_message_id
```

Используется метод:

imbot.v2.Chat.Message.send
```

Пример ключевых полей:

```json
{
  "botId": "123",
  "botToken": "generated-secret",
  "dialogId": "chat123",
  "message": "Новый счет...",
  "keyboard": {
    "BOT_ID": "123",
    "BUTTONS": [
      {
        "TEXT": "Оплатить",
        "COMMAND": "confirm",
        "COMMAND_PARAMS": "{\"token\":\"opaque-token\",\"mode\":\"pay\"}",
        "BLOCK": "Y"
      }
    ]
  }
}
```

Текст сообщения ограничивается 20 000 символами.

`remote_message_id` сохраняется локально и используется для последующего обновления.

## 6. Изменение статуса счета

После оплаты или отмены отметки доменный сервис создаёт событие вида:

```text
state:{revision}
```

Worker вызывает:

imbot.v2.Chat.Message.update
```

Обновление содержит тот же `BOT_ID` в keyboard, чтобы команда продолжала корректно маршрутизироваться.

Сбой редактирования сообщения не отменяет оплату и не создаёт второй чек. Delivery получает retry/uncertain статус согласно общей политике worker.

## 7. Входящие события и подтверждение

Bitrix24 работает в `fetch` режиме. Отдельный публичный Bitrix callback endpoint не нужен.

Docker запускает отдельный процесс:

```text
bitrix-events -> python -m app.bitrix24_runner
```

Обычный `worker` продолжает заниматься delivery и не блокируется долгим `Event.get`.

### Event.get

Первый запрос не содержит `offset`:

```json
{
  "botId": "123",
  "botToken": "generated-secret",
  "limit": 1000
}
```

После обработки страницы backend сохраняет `nextOffset` в `IntegrationConnection.server_revision`.

Следующий запрос использует:

```json
{
  "botId": "123",
  "botToken": "generated-secret",
  "offset": "next-offset",
  "limit": 1000
}
```

Если `hasMore` равен `true`, runner продолжает обработку страниц с паузой не менее двух секунд. Если `false`, новый цикл начинается через пять секунд.

### Durable inbox

Каждое событие сохраняется в `IntegrationInbox` по ключу:

```text
(connection_id, eventId)
```

Порядок обработки:

1. получить страницу Bitrix24;
2. сохранить event в inbox;
3. обработать действие и сохранить result;
4. сделать commit;
5. только после этого сохранить `nextOffset`;
6. перейти к следующей странице.

Повторное событие не оплачивает счет повторно.

## 8. Обработка нажатия кнопки

Ожидаемое событие:

```text
ONIMBOTV2COMMANDADD
```

Backend принимает только:

{
  "type": "ONIMBOTV2COMMANDADD",
  "botId": "123",
  "command": {
    "command": "confirm",
    "context": "keyboard",
    "params": "{\"token\":\"opaque-token\",\"mode\":\"pay\"}"
  },
  "message": {
    "id": "message-id"
  },
  "chat": {
    "dialogId": "chat123"
  },
  "user": {
    "id": "user-id"
  }
}
```

Проверяются:

- тип события;
- `context == keyboard`;
- команда `confirm`;
- bot ID текущего connection;
- dialog ID текущего channel;
- message ID исходной Delivery;
- action token;
- активность connection/channel;
- срок действия action;
- текущая revision occurrence;
- одноразовое использование token.

После этого вызывается:

```python
confirm_occurrence(
    occurrence_id,
    actor=f"bitrix24:{connection_id}:{user_id}",
    paid=True,
    demo=True,
    expected_revision=action.revision,
)
```

Внешний пользователь не получает web account и административные права.

Для подтверждения команды runner вызывает:

imbot.v2.Command.answer
```

## 9. Отправка чека

После успешной оплаты создаётся обычный демонстрационный receipt Invoice Keeper. Worker загружает HTML-чек через:

imbot.v2.File.upload
```

Передаётся Base64 без `data:` prefix:

```json
{
  "botId": "123",
  "botToken": "generated-secret",
  "dialogId": "chat123",
  "fields": {
    "name": "DEMO-20261024.html",
    "content": "base64-content"
  },
  "message": "Демонстрационный чек"
}
```

Bitrix24 ограничивает файл 100 MB. Реальный Invoice Keeper HTML-чек существенно меньше этого лимита.

Ошибка загрузки файла не отменяет оплату и не создаёт новый receipt.

## 10. Frontend API flow

Frontend wizard должен выполнять следующие запросы:

```text
GET  /api/v1/integrations/providers
POST /api/v1/integrations/connections
POST /api/v1/integrations/connections/{id}/validate
POST /api/v1/integrations/connections/{id}/setup
POST /api/v1/channels
```

Для Bitrix24 frontend показывает:

- название connection;
- portal URL;
- incoming webhook URL как password/write-only поле;
- статус проверки;
- статус setup;
- `external_bot_id` после setup;
- поле `remote_chat_id` для dialog ID.

Frontend никогда не получает и не сохраняет обратно `webhook_url` или `bot_token`.

Пример создания connection:

```ts
await api.post('/api/v1/integrations/connections', {
  provider: 'bitrix24',
  name: 'Bitrix24 компании',
  base_url: 'https://company.bitrix24.ru',
  credentials: {
    webhook_url: webhookUrl,
  },
})
```

После setup карточка показывает только:

```json
{
  "provider": "bitrix24",
  "setup_status": "ready",
  "health_status": "healthy",
  "has_credentials": true,
  "external_bot_id": "123"
}
```

## 11. Ошибки

Frontend не должен показывать raw Bitrix response.

Основные состояния:

| Состояние | Значение |
|---|---|
| `credentials_invalid` | webhook или bot credentials отсутствуют/неверны |
| `provider_rejected` | Bitrix24 отклонил запрос |
| `provider_invalid_response` | ответ не соответствует контракту |
| `file_too_large` | файл больше 100 MB |
| `invalid_callback` | событие не является разрешённой keyboard-командой |
| `setup_status=configured` | connection создан, bot setup не завершён |
| `setup_status=ready` | bot и команда зарегистрированы |
| `health_status=error` | последнее обращение к Bitrix24 завершилось ошибкой |
| `Delivery.status=uncertain` | результат внешней отправки неизвестен |

Ошибки `uncertain` нельзя автоматически превращать в слепой повтор: Bitrix24 мог уже принять сообщение.

## 12. Docker и production

В `.env` необходимо задать:

```dotenv
INTEGRATIONS_ENCRYPTION_KEY=stable-fernet-key
PUBLIC_APP_URL=https://invoice.example.com
```

Запуск:

```powershell
docker compose up -d --build
```

Сервисы:

```text
api            HTTP API
worker         Delivery queue, Telegram/email/webhook and chat updates
bitrix-events  Bitrix24 Event.get polling
bot            optional Telegram polling profile
```

Нужно запустить только один `bitrix-events` runner для конкретной базы и набора Bitrix connections. Два одинаковых runner процесса могут конкурировать за одни и те же events.

## 13. Live smoke checklist

Mock contract tests не проверяют реальные права конкретного портала. Перед production нужно проверить:

1. incoming webhook создан со scope `imbot`;
2. `validate` получает revision;
3. `setup` регистрирует bot и command;
4. бот добавляется в тестовый chat;
5. `remote_chat_id` корректен;
6. новый счет приходит в Bitrix24;
7. кнопка отображается и отправляет keyboard command;
8. occurrence становится оплаченной один раз;
9. HTML-чек приходит в чат;
10. сообщение обновляется после оплаты;
11. повторное событие не создаёт вторую оплату;
12. после restart cursor продолжает обработку без потери events.

До выполнения этого списка статус интеграции: «проверено по официальному контракту и mock-тестам; требуется проверка на конкретном Bitrix24 портале».

## 14. Официальные источники

- [Chatbots 2.0](https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/index.html)
- [Revision.get](https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/imbot.v2/revision-get.html)
- [Bot.register](https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/imbot.v2/bots/bot-register.html)
- [Event.get](https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/imbot.v2/events/event-get.html)
- [Events](https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/imbot.v2/events/events.html)
- [Command.register](https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/imbot.v2/commands/command-register.html)
- [Message keyboards](https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/imbot.v2/messages/message-keyboards.html)
- [File.upload](https://apidocs.bitrix24.com/api-reference/chat-bots/chat-bots-v2/imbot.v2/files/file-upload.html)
