# Code Review: Downloader-Bot

Дата review: 2026-04-06  
Скоуп: `config.py`, `main.py`, `handlers/`, `middlewares/`, `services/`, `utils/`, Docker/CI конфігурація.  
Актуальна перевірка backlog-run: `.venv\Scripts\python -m pytest -q` -> `243 passed, 6 skipped`.

## 1. Огляд архітектури

### Що в проєкті організовано добре

- Є зрозумілий поділ на `handlers`, `middlewares`, `services`, `utils`, `messages`, `keyboards`.
- Для heavy-download сценаріїв уже є окремий `AdaptiveDownloadQueue` та `ResilientDownloader`.
- Логування централізоване в [`log/logger.py`](log/logger.py) з контекстом, perf/event логами та JSONL.
- Є непогане тестове покриття для основних сценаріїв та CI workflow.

### Основні архітектурні проблеми

| Severity | Файл / рядок | Проблема | Рекомендація |
|---|---|---|---|
| High | `handlers/user.py:22`, `handlers/tiktok.py:51`, `handlers/youtube.py:45`, `handlers/instagram.py:50`, `handlers/twitter.py:47`, `handlers/pinterest.py:45`, `handlers/soundcloud.py:42`, `handlers/admin.py:25`, `middlewares/chat_tracker.py:8`, `middlewares/ban_middleware.py:7` | Шари сильно зв'язані через `from main import bot, db, send_analytics`. Це робить `main.py` service locator-ом, створює циклічні залежності, погіршує тестованість і ускладнює повторне використання логіки поза ботом. | Винести `bot`, `db`, analytics client і конфіг в окремий контейнер залежностей або модуль `app_context.py`; передавати сервіси в handlers/middlewares через конструктор або фабрики роутерів. |
| High | `handlers/tiktok.py:1`, `handlers/youtube.py:1`, `handlers/instagram.py:1`, `handlers/twitter.py:1`, `handlers/pinterest.py:1`, `handlers/user.py:1`, `handlers/utils.py:1` | Дуже великі модулі: `tiktok.py` ~1364 рядки, `youtube.py` ~1200, `instagram.py` ~1155, `twitter.py` ~1081, `pinterest.py` ~1000, `handlers/utils.py` ~850. В одному файлі змішані transport logic, parsing, download orchestration, UI response, inline mode, caching. | Розрізати кожну платформу мінімум на: `parsing`, `service/client`, `delivery`, `inline`, `callbacks`. Для спільної логіки зробити базові reusable service-функції. |

## 2. Якість коду

### Основні спостереження

- Проєкт має сильні сторони: хороша кількість тестів, непогано централізоване логування, є спроба ізолювати HTTP/DB/download шар.
- Головна проблема якості коду зараз не в стилі, а в масштабі модулів, дублюванні і великій кількості "best effort" обробників помилок, які ховають дефекти.

### Знайдені проблеми

| Severity | Файл / рядок | Проблема | Рекомендація |
|---|---|---|---|
| Medium | `handlers/tiktok.py`, `handlers/instagram.py`, `handlers/pinterest.py`, `handlers/twitter.py`, `handlers/youtube.py`, `handlers/soundcloud.py` | Після винесення shared helpers для resolved user settings, throttled progress updates і retry-status callbacks ще лишається дублювання у media upload/caching та частині inline delivery flow. Воно все ще збільшує вартість змін і ризик роз'їзду поведінки між платформами. | Продовжити уніфікацію навколо спільного upload/cache/send pipeline і менших reusable delivery helper-функцій. |
| Medium | `handlers/admin.py`, `handlers/user.py`, `handlers/tiktok.py`, `handlers/instagram.py`, `handlers/twitter.py`, `handlers/pinterest.py`, `handlers/soundcloud.py`, `utils/download_manager.py` | По коду багато широких `except Exception`, часто без переведення в чіткий доменний результат. Через це важко відрізнити реальну бізнес-помилку від дефекту в коді. | Залишати broad catch тільки на boundary layer; усередині сервісів ловити конкретні винятки і логувати структуровано. |

## 3. Потенційні баги

Наразі окремих незакритих correctness-багів із першого проходу в backlog не лишилось; поточні залишки вже більше про архітектуру, performance та operational hardening.

## 4. Безпека

| Severity | Файл / рядок | Проблема | Рекомендація |
|---|---|---|---|

## 5. Продуктивність

| Severity | Файл / рядок | Проблема | Рекомендація |
|---|---|---|---|

## 6. SOLID та чиста архітектура

| Принцип | Де порушується | Що саме не так | Рекомендація |
|---|---|---|---|
| SRP | `handlers/tiktok.py`, `handlers/youtube.py`, `handlers/instagram.py`, `handlers/twitter.py`, `handlers/pinterest.py`, `handlers/soundcloud.py` | Один модуль робить все: парсинг URL, network fetch, retry, queue orchestration, message formatting, inline mode, callback processing, file cleanup. | Розбити по ролях: parser/client/service/delivery/callbacks. |
| DIP | Усі `from main import ...` імпорти | Бізнес-логіка залежить від конкретного runtime entrypoint, а не від абстракцій. | Впровадити dependency injection або application context. |
| OCP | Повторювані platform handlers | Додавання нової платформи майже гарантовано копіює ще 500-1000 рядків існуючого шаблону. | Стандартизувати platform adapter interface і спільний download/send pipeline. |
| Clean Architecture boundary | `handlers/*`, `services/db.py` | Telegram-specific `Message` / `Bot` і runtime wiring все ще протікають у бізнес-потоки і ускладнюють ізоляцію логіки. DB lifecycle вже переведений на Alembic-first init, але transport/infrastructure межі залишаються розмитими. | Відокремити transport DTO від core service layer і далі прибирати прямі runtime-залежності з handlers. |

### Загальний висновок по SOLID

Проєкт уже має хороші "цеглинки" для cleaner architecture: queue, downloader, logger, db-service. Але вони зараз використовуються з дуже товстими handler-модулями, тому SRP і DIP порушуються найсильніше. Найбільший виграш дасть не дрібний стильовий рефакторинг, а виділення спільного platform pipeline і прибирання залежності від `main`.

## 7. Пріоритетний список змін

### Важливо

1. Доробити persistence для тимчасових workflow-сховищ: TTL/LRU вже є, але важливі токени й pending flow все ще губляться після рестарту.

### Бажано

1. Розбити великі platform handlers на менші модулі.
2. Далі уніфікувати дубльовану логіку upload/caching/delivery flow між платформами.
4. Прибрати `from main import ...` через окремий app context / dependency container.

## Підсумок

Критичні correctness/security проблеми з першого проходу вже закриті. Поточний backlog тепер здебільшого про:

1. архітектурне розчеплення модулів і залежностей;
2. доведення тимчасових workflow до більш production-safe стану;
3. cleanup технічного боргу навколо залежностей і startup lifecycle.
