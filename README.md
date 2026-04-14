# Микросервис анализа UI-скриншотов

Flask-сервис принимает изображение интерфейса, отправляет его в Mistral и сохраняет job, input, result, artifacts и logs в PostgreSQL. Загруженные изображения сохраняются в локальное хранилище `storage/images/`.

## Запуск

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env
python .\testUpload.py
```

Сервис по умолчанию поднимается на `http://localhost:8001`.

## Переменные окружения

- `MISTRAL_API_KEY` или `MISTRAL_AI_API_KEY`
- `PORT=8001`
- `MAX_FILE_SIZE_MB=10`
- `LOG_LEVEL=INFO`
- `CORS_ALLOWED_ORIGINS=`
- `POSTGRES_HOST=localhost`
- `POSTGRES_PORT=8080`
- `POSTGRES_DB=test_platform`
- `POSTGRES_USER=postgres`
- `POSTGRES_PASSWORD=postgres`
- `STORAGE_ROOT=storage`

Можно задать `DATABASE_URL`, тогда он будет использован вместо отдельных `POSTGRES_*`.

## PostgreSQL

Подключение по умолчанию:

- host: `localhost`
- port: `8080`
- database: `test_platform`
- username: `postgres`
- password: `postgres`

SQL-схема лежит в [schema.sql](/C:/Users/artem/OneDrive/Desktop/TestPhotoAnalysis/schema.sql). Приложение также создаёт таблицы автоматически через SQLAlchemy при старте.

## API

### `GET /health`

```json
{
  "status": "UP",
  "service": "photo-analysis"
}
```

### `POST /upload-image`

Принимает `multipart/form-data` с обязательным полем `image`. Старый контракт сохранён:

```json
{
  "status": "success",
  "gherkin": "Feature: ..."
}
```

Что происходит внутри:

- создаётся `Job` со статусом `pending`, затем `processing`
- сохраняется `JobInput` с metadata файла
- файл пишется в `storage/images/`
- создаётся `Artifact` типа `uploaded_image`
- в `JobLog` пишутся события `file uploaded`, `ai request started`, `ai request finished` или `ai request failed`
- при успехе создаётся `JobResult`, job переводится в `done`
- при ошибке job переводится в `failed`

### `GET /jobs/<job_id>`

Возвращает metadata job.

### `GET /jobs/<job_id>/result`

Возвращает Gherkin и `result_json`.

### `GET /jobs/<job_id>/artifacts`

Возвращает список артефактов job.

### `GET /jobs`

Поддерживает query-параметры:

- `service_type`
- `status`
- `limit`
- `offset`

### `GET /get-test-cases`

Для совместимости возвращает последний успешный Gherkin из PostgreSQL.

### `GET /jobs/<job_id>/feature`

Скачивает результат как `.feature`.

## Валидация и ошибки

Backend валидирует:

- наличие поля `image`
- имя файла
- MIME вида `image/*`
- пустой файл
- лимит размера файла

Ошибки возвращаются в формате:

```json
{
  "success": false,
  "error": {
    "code": "FILE_TOO_LARGE",
    "message": "Uploaded file exceeds the 10 MB limit"
  }
}
```
