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
| Medium | `services/db.py:126-143` | Проєкт має Alembic, але стартова ініціалізація БД використовує `Base.metadata.create_all`. Це обминає історію міграцій і небезпечно для еволюції схеми в production. | На старті виконувати лише `alembic upgrade head`; `create_all` залишити тільки для тестів/local bootstrap. |

## 2. Якість коду

### Основні спостереження

- Проєкт має сильні сторони: хороша кількість тестів, непогано централізоване логування, є спроба ізолювати HTTP/DB/download шар.
- Головна проблема якості коду зараз не в стилі, а в масштабі модулів, дублюванні і великій кількості "best effort" обробників помилок, які ховають дефекти.

### Знайдені проблеми

| Severity | Файл / рядок | Проблема | Рекомендація |
|---|---|---|---|
| Medium | `handlers/tiktok.py:175`, `handlers/instagram.py:155`, `handlers/pinterest.py:213`, `handlers/soundcloud.py:184` | Дублюється однакова логіка `get_user_settings`. Подібне дублювання є і в inline-send flow, media upload flow, status-message flow. Це збільшує вартість змін і ризик роз'їзду поведінки між платформами. | Винести спільні helper/service функції для settings, queue/progress handling, upload/caching, inline token lifecycle. |
| Medium | `services/db.py:149-151`, `services/db.py:78-97` | Є мертвий або змішаний код: `get_session()` з коментарем про FastAPI, хоча проєкт не FastAPI; `run_alembic_migration()` не використовується. Це створює шум і плутає реальний runtime path. | Видалити невикористаний код або перемістити його в dev tools/scripts. |
| Medium | `handlers/admin.py`, `handlers/user.py`, `handlers/tiktok.py`, `handlers/instagram.py`, `handlers/twitter.py`, `handlers/pinterest.py`, `handlers/soundcloud.py`, `utils/download_manager.py` | По коду багато широких `except Exception`, часто без переведення в чіткий доменний результат. Через це важко відрізнити реальну бізнес-помилку від дефекту в коді. | Залишати broad catch тільки на boundary layer; усередині сервісів ловити конкретні винятки і логувати структуровано. |
| Low | `config.py:7-24` | Naming/конфіг змішані: частина змінних у верхньому регістрі, частина в нижньому (`admin_id`, `custom_api_url`). Це дрібниця, але збільшує ентропію в коді. | Привести весь конфіг до одного стилю (`UPPER_SNAKE_CASE`) і однієї точки валідації. |

## 3. Потенційні баги

| Severity | Файл / рядок | Проблема | Рекомендація |
|---|---|---|---|
| Medium | `middlewares/private_chat_guard.py:13-18` | `SUPPORTED_LINK_RE` не покриває `soundcloud` та `pinterest`, хоча проєкт їх підтримує. Guard буде працювати нерівномірно залежно від платформи. | Винести URL detection в спільний реєстр платформ і використовувати його і в middleware, і в handlers. |

## 4. Безпека

| Severity | Файл / рядок | Проблема | Рекомендація |
|---|---|---|---|
| Medium | `main.py:84-101` | У Google Analytics відправляються сирі Telegram `user_id` як `client_id`, `user_id` і `session_id`. Це прямий витік стабільного зовнішнього ідентифікатора в third-party сервіс. | Хешувати/псевдонімізувати user id перед відправкою або взагалі відмовитись від передачі зовнішніх ID у GA. |
| Medium | `requirements.txt:1-14` | Залежності не зафіксовані по версіях. Це відкриває supply-chain та reproducibility ризики: одна й та сама ревізія коду сьогодні й завтра може отримати різні пакети. | Зафіксувати версії (`==` або принаймні контрольовані діапазони), додати lockfile/constraints та регулярний dependency update process. |

## 5. Продуктивність

| Severity | Файл / рядок | Проблема | Рекомендація |
|---|---|---|---|
| Medium | `handlers/admin.py:160-223`, `handlers/admin.py:371-413` | `check_active_users` і масова розсилка працюють строго послідовно з `sleep(0.05)` на кожного користувача. На великій базі це буде дуже повільно і схильне до таймаутів/429. | Додати bounded concurrency (наприклад, semaphore 5-10), retry policy і окремий background job для довгих адмінських операцій. |
| Medium | `main.py:84-101`, `main.py:116-139` | Analytics batch flush відправляє події в GA послідовно по одній. При зростанні трафіку це створить вузьке місце і збільшить шанс переповнення `_analytics_queue`. | Або використовувати batch endpoint/паралельну відправку з лімітом concurrency, або спочатку тільки persist в БД, а експорт в GA робити окремим воркером. |

## 6. SOLID та чиста архітектура

| Принцип | Де порушується | Що саме не так | Рекомендація |
|---|---|---|---|
| SRP | `handlers/tiktok.py`, `handlers/youtube.py`, `handlers/instagram.py`, `handlers/twitter.py`, `handlers/pinterest.py`, `handlers/soundcloud.py` | Один модуль робить все: парсинг URL, network fetch, retry, queue orchestration, message formatting, inline mode, callback processing, file cleanup. | Розбити по ролях: parser/client/service/delivery/callbacks. |
| DIP | Усі `from main import ...` імпорти | Бізнес-логіка залежить від конкретного runtime entrypoint, а не від абстракцій. | Впровадити dependency injection або application context. |
| OCP | Повторювані platform handlers | Додавання нової платформи майже гарантовано копіює ще 500-1000 рядків існуючого шаблону. | Стандартизувати platform adapter interface і спільний download/send pipeline. |
| Clean Architecture boundary | `services/db.py:126-143`, `handlers/*` | Infrastructure рішення (`create_all`, telegram-specific `Message`, `Bot`) протікають у бізнес-потоки і ускладнюють ізоляцію логіки. | Відокремити transport DTO від core service layer; міграції і startup init винести окремо. |

### Загальний висновок по SOLID

Проєкт уже має хороші "цеглинки" для cleaner architecture: queue, downloader, logger, db-service. Але вони зараз використовуються з дуже товстими handler-модулями, тому SRP і DIP порушуються найсильніше. Найбільший виграш дасть не дрібний стильовий рефакторинг, а виділення спільного platform pipeline і прибирання залежності від `main`.

## 7. Пріоритетний список змін

### Важливо

1. Доробити persistence для тимчасових workflow-сховищ: TTL/LRU вже є, але важливі токени й pending flow все ще губляться після рестарту.
2. Додати bounded concurrency для `check_active_users` і масової розсилки.
3. Переробити analytics export: прибрати сирі Telegram ID з GA payload і перестати штовхати події по одній.
4. Закрити нерівномірний private-link detection для `soundcloud` і `pinterest`.

### Бажано

1. Розбити великі platform handlers на менші модулі.
2. Уніфікувати дубльовану логіку settings/progress/upload/inline flow.
3. Перейти з `create_all` на повноцінний Alembic-first lifecycle.
4. Видалити або винести мертвий код (`run_alembic_migration`, `get_session`).
5. Зафіксувати версії залежностей і додати регулярний dependency audit.
6. Прибрати `from main import ...` через окремий app context / dependency container.

## Підсумок

Критичні correctness/security проблеми з першого проходу вже закриті. Поточний backlog тепер здебільшого про:

1. архітектурне розчеплення модулів і залежностей;
2. доведення тимчасових workflow до більш production-safe стану;
3. cleanup технічного боргу навколо analytics, залежностей і startup lifecycle.
