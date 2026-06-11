# ICP Monitor 🇪🇸👮

Автоматический мониторинг сайта записи в полицию Испании.  
Отправляет уведомление в Telegram когда номер лота изменился.

## Как работает

1. **GitHub Actions** запускает скрипт каждые 15 минут  
2. Скрипт заходит на [icp.administracionelectronica.gob.es](https://icp.administracionelectronica.gob.es/icpplus/index.html) через Playwright (настоящий браузер)  
3. Выбирает провинцию → комиссарию → трамит  
4. Читает текст со страницы результата (номер лота или "нет записей")  
5. Сравнивает с предыдущим значением — если изменилось, шлёт в Telegram  
6. Если текущий лот ≥ ожидаемого — уведомление выделяется 🎯

---

## Пошаговая настройка

### Шаг 1. Создать Telegram бота

1. Напиши [@BotFather](https://t.me/BotFather) → `/newbot`  
2. Придумай имя и username (например `icp_oviedo_bot`)  
3. Сохрани **токен** вида `123456789:ABCdef...`

### Шаг 2. Узнать свой Chat ID

1. Напиши [@userinfobot](https://t.me/userinfobot) — он ответит твоим ID  
2. Сохрани число, например `123456789`

### Шаг 3. Узнать коды провинции и комиссарии

Зайди на сайт руками, открой DevTools (F12) → Elements:

1. Выбери провинцию в `<select id="form">` — посмотри `value` нужного `<option>`  
   (например, Asturias = `"33"`)
2. После перехода выбери комиссарию в `<select id="sede">` — посмотри `value`  
   (например, Comisaría de Oviedo = `"12345"`)

Либо: URL страницы после выбора часто содержит эти коды.

### Шаг 4. Сформировать WATCHLIST_JSON

Это JSON-список объектов. Пример для одного участка:

```json
[
  {
    "province": "33",
    "province_name": "Asturias",
    "office": "11354",
    "office_name": "Oviedo",
    "tramite": "RECOGIDA",
    "expected_lot": "150"
  }
]
```

Для нескольких участков — просто добавь объекты в массив:

```json
[
  {
    "province": "33",
    "province_name": "Asturias",
    "office": "11354",
    "office_name": "Oviedo",
    "tramite": "RECOGIDA",
    "expected_lot": "150"
  },
  {
    "province": "28",
    "province_name": "Madrid",
    "office": "28007",
    "office_name": "Madrid Centro",
    "tramite": "HUELLAS",
    "expected_lot": ""
  }
]
```

Поле `tramite` — ключевое слово из названия трамита (не обязательно полное).  
Поле `expected_lot` — оставь `""` если не хочешь особых уведомлений.

### Шаг 5. Форкнуть репозиторий

1. Залей код на GitHub (или форкни этот репо)  
2. Структура должна быть:
   ```
   .github/workflows/monitor.yml
   monitor.py
   requirements.txt
   data/           ← создастся автоматически
   ```

### Шаг 6. Добавить GitHub Secrets

Зайди: **Settings → Secrets and variables → Actions → New repository secret**

| Имя | Значение |
|-----|----------|
| `TELEGRAM_TOKEN` | токен от BotFather |
| `TELEGRAM_CHAT_ID` | твой chat ID |
| `WATCHLIST_JSON` | JSON из шага 4 (однострочный) |

### Шаг 7. Запустить вручную первый раз

1. Зайди в **Actions → ICP Monitor → Run workflow**  
2. Посмотри логи — убедись что скрипт прошёл все шаги  
3. Проверь что пришло первое уведомление в Telegram  

После этого бот будет запускаться автоматически каждые 15 минут.

---

## Опциональный локальный бот для управления

Если хочешь управлять списком через Telegram:

```bash
pip install python-telegram-bot playwright
playwright install chromium
export TELEGRAM_TOKEN="..."
python bot.py
```

Команды:
- `/add` — добавить участок (пошаговый визард)
- `/list` — показать список
- `/remove` — убрать участок
- `/export` — получить WATCHLIST_JSON для вставки в GitHub Secrets

---

## Как читать уведомления

```
🔔 Изменение на ICP!
📍 Asturias → Oviedo
📋 RECOGIDA

📌 Было: No hay citas disponibles en este momento
✅ Стало: Lote 142 disponible

🕐 15.06.2024 09:30
🔗 Открыть сайт
```

Если лот совпал или превысил ожидаемый:
```
🎯 ВАШ ЛОТ НАЙДЕН!
🔔 Изменение на ICP!
...
```

---

## Частота проверки

По умолчанию: каждые 15 минут (cron `*/15 * * * *`).  
Можно изменить в `.github/workflows/monitor.yml`.  

> ⚠️ GitHub Actions бесплатно даёт 2000 минут/месяц.  
> Каждый запуск занимает ~3-5 мин → 96 запусков/день × 4 мин ≈ 384 мин/день ≈ 11500 мин/месяц.  
> **Для публичного репозитория Actions бесплатны без ограничений!**  
> Сделай репозиторий публичным, или уменьши частоту до `*/30 * * * *`.

---

## Troubleshooting

**Скрипт не находит трамит:**  
Зайди на сайт руками, посмотри точное название трамита, обнови ключевое слово в `WATCHLIST_JSON`.

**Telegram молчит:**  
Проверь что написал боту хоть одно сообщение (иначе бот не может писать первым).  
Проверь TELEGRAM_TOKEN и TELEGRAM_CHAT_ID в Secrets.

**403 на сайте:**  
Сайт изредка блокирует запросы. Обычно проходит само. Если постоянно — попробуй изменить user-agent в `monitor.py`.
