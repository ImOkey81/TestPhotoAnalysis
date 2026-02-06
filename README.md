### Микросервис преобразования картинки в Gherkin
## Запуск
```
 python .\testUpload.py
 ```

Или

```
docker build -t image_to_gherkin .
docker run image_to_gherkin
```

## Пример запроса
```
curl -X POST "http://localhost:8001/generate-gherkin" \
-H "Content-Type: application/json" \
-d '{
  "description": "Пользователь вводит логин и пароль на странице авторизации",
  "base64_image": "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCABkAGQDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD3+iiigD//2Q==",
  "image_type": "image/jpg"
}'
```
## Пример ответа
```
{
    "gherkin": "```gherkin\nFeature: Авторизация пользователя на странице входа
      
      Scenario: Пользователь вводит логин и пароль
          Given пользователь находится на странице авторизации
          When пользователь вводит логин \"exampleUser\"
          And пользователь вводит пароль \"examplePassword\"
          Then система должна проверить корректность введенных данных
          And система должна предоставить доступ к личному кабинету при успешной авторизации
          Or система должна показать сообщение об ошибке при неверных данных\n```"
}
```