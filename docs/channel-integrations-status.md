# Статус интеграций VK Teams, Mattermost и Bitrix24

## Реализовано

- подключения провайдеров с зашифрованными credentials через `INTEGRATIONS_ENCRYPTION_KEY`;
- admin API для создания, изменения, проверки и просмотра статуса подключений;
- каналы `vk_teams` и `mattermost` с `connection_id` и `remote_chat_id`;
- отправка и редактирование счетов через worker;
- отправка демонстрационного HTML-чека после подтверждения;
- Mattermost interactive callback;
- VK Teams polling через `events/get` с cursor в `server_revision`;
- durable inbound deduplication через `integration_inbox`;
- одноразовые action-токены с проверкой провайдера, подключения, чата, сообщения, revision и срока действия;
- оба провайдера используют общий `confirm_occurrence`.
- Bitrix24 Chatbots 2.0 (`imbot.v2`) с encrypted incoming webhook, admin-only setup, fetch polling и `IntegrationInbox` deduplication;
- Bitrix24 отправляет/редактирует счета, загружает HTML-чек и принимает только keyboard-команду `confirm` с непрозрачным action-токеном.

## API

- `GET /api/v1/integrations/providers`
- `GET /api/v1/integrations/connections`
- `POST /api/v1/integrations/connections`
- `GET/PATCH /api/v1/integrations/connections/{connection_id}`
- `POST /api/v1/integrations/connections/{connection_id}/validate`
- `POST /api/v1/integrations/connections/{connection_id}/setup` (только Bitrix24)
- `GET /api/v1/integrations/connections/{connection_id}/status`
- `POST /api/v1/integrations/mattermost/{connection_id}/actions`
- `POST /api/v1/integrations/vk-teams/{connection_id}/events`

Все admin endpoints требуют роль `admin`. Bitrix24 fetch mode не открывает callback endpoint: worker получает `imbot.v2.Event.get`, сохраняет event/result и только затем продвигает offset.

## Ограничения

Live smoke пока не запускался: для Bitrix24 нужны портал, incoming webhook со scope `imbot`, бот с правом писать в целевой чат и запущенный worker. Контрактные mock-тесты не подтверждают portal permissions, rate limits или фактическое поведение `imbot.v2` в конкретном тарифе.

Миграция интеграций находится в `0007_chat_integrations.py`, потому что backend-коммит с профилями занял `0006`.
