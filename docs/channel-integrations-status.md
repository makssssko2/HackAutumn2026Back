# Статус интеграций VK Teams и Mattermost

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

## API

- `GET /api/v1/integrations/providers`
- `GET /api/v1/integrations/connections`
- `POST /api/v1/integrations/connections`
- `GET/PATCH /api/v1/integrations/connections/{connection_id}`
- `POST /api/v1/integrations/connections/{connection_id}/validate`
- `GET /api/v1/integrations/connections/{connection_id}/status`
- `POST /api/v1/integrations/mattermost/{connection_id}/actions`
- `POST /api/v1/integrations/vk-teams/{connection_id}/events`

Все admin endpoints требуют роль `admin`. Callback endpoints не требуют веб-сессии, но принимают только выданные приложением action-токены и дополнительно проверяют connection/channel/message context; Mattermost должен иметь публичный callback URL.

## Ограничения

Live smoke пока не запускался: нужны реальные токены VK Teams, Mattermost bot account, доступный Mattermost URL и публичный `PUBLIC_APP_URL`. Контрактные mock-тесты обоих адаптеров проходят.

Миграция интеграций находится в `0007_chat_integrations.py`, потому что backend-коммит с профилями занял `0006`.
