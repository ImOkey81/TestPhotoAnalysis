import os
import base64
import time
import random
import threading
import requests
import cv2
import numpy as np
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS

load_dotenv()

app = Flask(__name__)
CORS(app, supports_credentials=True)

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY")

if not MISTRAL_API_KEY:
    raise RuntimeError("MISTRAL_API_KEY not found in .env")

HEADERS = {
    "Authorization": f"Bearer {MISTRAL_API_KEY}",
    "Content-Type": "application/json",
}

MISTRAL_LOCK = threading.Lock()


class ImageTestGenerator:
    def __init__(self):
        self.generated_test_cases: str | None = None
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

        # cooldown после 429, чтобы не биться о лимит сразу снова
        self.cooldown_until = 0.0
        self.cooldown_lock = threading.Lock()

    def preprocess_image(self, image_bytes: bytes) -> tuple[bytes, str]:
        """
        Сжимает/уменьшает изображение, чтобы легче проходить лимиты.
        Возвращает (jpeg_bytes, content_type)
        """
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            # если вдруг не декодируется — отправим как есть
            return image_bytes, "application/octet-stream"

        h, w = img.shape[:2]
        max_w = 1024
        if w > max_w:
            scale = max_w / float(w)
            new_w = int(w * scale)
            new_h = int(h * scale)
            img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ok:
            return image_bytes, "application/octet-stream"

        return buf.tobytes(), "image/jpeg"

    def _apply_cooldown_if_needed(self):
        with self.cooldown_lock:
            now = time.time()
            if now < self.cooldown_until:
                time.sleep(self.cooldown_until - now)

    def _set_cooldown(self, seconds: float):
        with self.cooldown_lock:
            self.cooldown_until = max(self.cooldown_until, time.time() + seconds)

    def call_mistral_with_retry(self, payload: dict, timeout: int = 60, max_attempts: int = 8):
        last_status = None
        last_text = None
        last_headers = None

        for attempt in range(1, max_attempts + 1):
            try:
                self._apply_cooldown_if_needed()

                with MISTRAL_LOCK:
                    resp = self.session.post(MISTRAL_API_URL, json=payload, timeout=timeout)

                last_status = resp.status_code
                last_text = resp.text
                last_headers = dict(resp.headers)

                if resp.status_code == 429:
                    # ставим cooldown 5–10 секунд, чтобы не ловить 429 пачкой
                    self._set_cooldown(random.uniform(5.0, 10.0))

                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        sleep_s = int(retry_after)
                    else:
                        # более “мягкий” backoff, но с потолком
                        base = min(2 ** (attempt - 1), 30)
                        sleep_s = base + random.uniform(0.5, 1.5)

                    time.sleep(sleep_s)
                    continue

                if 500 <= resp.status_code < 600:
                    base = min(2 ** (attempt - 1), 30)
                    time.sleep(base + random.uniform(0.5, 1.5))
                    continue

                resp.raise_for_status()
                return resp.json(), None

            except requests.RequestException as e:
                last_status = last_status or "network_error"
                last_text = str(e)
                base = min(2 ** (attempt - 1), 30)
                time.sleep(base + random.uniform(0.5, 1.5))
                continue

        return None, {"status": last_status, "body": last_text, "headers": last_headers}

    def generate_gherkin(self, image_bytes: bytes, content_type: str):
        # 1) сжимаем изображение (важно!)
        compressed_bytes, forced_type = self.preprocess_image(image_bytes)
        use_type = forced_type if forced_type.startswith("image/") else content_type

        base64_image = base64.b64encode(compressed_bytes).decode("utf-8")

        messages = [
            {
                "role": "system",
                "content": (
                    "Ты QA-инженер. Генерируешь тест-кейсы ТОЛЬКО по фактам, видимым на одном изображении UI.\n"
                    "ЖЁСТКИЕ ПРАВИЛА:\n"
                    "1) Запрещены любые предположения о поведении и действиях пользователя. НЕ используй слова/идеи: click, tap, hover, scroll, open, opens, navigate, redirect, play, playback, search, login, modal.\n"
                    "2) Разрешены только проверки snapshot текущего экрана: видимость/наличие элементов, читаемые тексты, названия секций, структура областей (левая панель/центральная область/верхняя часть/нижняя панель/правая часть), очевидные состояния (выделение/активный пункт), если это видно.\n"
                    "3) Если элемент не читается или ты не уверен — НЕ упоминай его.\n"
                    "4) Все числа/цены/проценты/счётчики/время/имена/прочие динамические значения заменяй на <value>.\n"
                    "5) НЕ используй точные координаты и сравнительные формулировки: top/right/left/bottom corner, 'справа от', 'слева от', 'ниже/выше', 'слева направо', 'в вертикальном порядке'.\n"
                    "6) НЕ интерпретируй иконки без текста и не перечисляй 'additional icons/controls'. Если у элемента нет читаемого текста — обычно не упоминай его.\n"
                    "7) Для карточек/плиток используй нейтральные формулировки: 'отображается минимум одна карточка' и 'карточка содержит текст <value>'. Не утверждай 'артист/страна/трек', если это не написано явно читаемым текстом.\n"
                    "8) Ответ строго в Gherkin. Без markdown. Без таблиц. Без комментариев.\n"
                    "9) Каждый Scenario должен содержать только шаги Then/And (без Given/When).\n"
                ),
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Сгенерируй 8–15 Scenario для проверки текущего экрана по этому изображению UI.\n"
                            "Только проверяемые по скриншоту утверждения (snapshot validation).\n"
                            "Покрой: левую панель (если видна), центральную область, верхнюю часть (если видна), нижнюю панель (если видна), заголовки/лейблы, основные секции и минимум одну карточку/плитку контента (если видны).\n"
                            "Сценарии должны быть устойчивыми к адаптивной верстке: избегай проверок точного порядка и точных координат.\n"
                            "Не используй действия пользователя и не описывай то, чего не видно на изображении."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": f"data:{use_type};base64,{base64_image}",
                    },
                ],
            },
        ]

        payload = {
            "model": "ministral-14b-2512",
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 1200,
        }

        data, err = self.call_mistral_with_retry(payload, timeout=60, max_attempts=8)
        if err:
            return None, err

        content = data["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.replace("```", "").strip()

        self.generated_test_cases = content
        return content, None


generator = ImageTestGenerator()


@app.route("/upload-image", methods=["POST"])
def upload_image():
    if "image" not in request.files:
        return jsonify({"error": "File field must be named 'image'"}), 400

    file = request.files["image"]

    if not file.content_type or not file.content_type.startswith("image/"):
        return jsonify({"error": "Only image files supported"}), 400

    try:
        image_bytes = file.read()
        gherkin, err = generator.generate_gherkin(image_bytes=image_bytes, content_type=file.content_type)

        if err:
            status = err.get("status")
            if status == 429:
                return jsonify({
                    "error": "Mistral rate limit exceeded. Try again in a few seconds.",
                    "details": {"status": status, "body": err.get("body")}
                }), 429

            if isinstance(status, int) and 500 <= status < 600:
                return jsonify({
                    "error": "Mistral temporary unavailable. Try again later.",
                    "details": {"status": status, "body": err.get("body")}
                }), 503

            return jsonify({
                "error": "Failed to generate gherkin",
                "details": {"status": status, "body": err.get("body")}
            }), 500

        return jsonify({"status": "success", "gherkin": gherkin})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/get-test-cases", methods=["GET"])
def get_test_cases():
    if generator.generated_test_cases:
        return jsonify({"status": "success", "gherkin": generator.generated_test_cases})
    return jsonify({"status": "error", "message": "Test cases have not been generated yet"}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8001)
