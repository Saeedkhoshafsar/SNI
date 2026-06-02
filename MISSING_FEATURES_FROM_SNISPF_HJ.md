# قابلیت‌هایی که SNISPF-HJ دارد و پروژه‌ی ما کم دارد

> **بخش مخصوص پروژه — لیست شکاف قابلیت‌ها (Feature-Gap)**
>
> این سند نتیجه‌ی **بررسی کامل و خط‌به‌خط** پروژه‌ی مرجع
> [`hjfisher/SNISPF-HJ`](https://github.com/hjfisher/SNISPF-HJ) است که ریشه‌ی مشترک
> با پروژه‌ی ماست ولی در مسیر متفاوتی توسعه یافته. هدف: مشخص‌کردن **همه‌ی چیزهایی که
> آن دارد و ما نداریم**، با توضیحات کامل، تا در جلسات بعد بتوانیم **استپ‌به‌استپ** پیاده کنیم.
>
> تاریخ بررسی: ۲۰۲۶-۰۶-۰۲ — منبع: کلون کامل ریپو در `/home/user/SNISPF-HJ-ref`

---

## ۰. خلاصه‌ی مدیریتی (TL;DR)

تفاوت بنیادی این دو پروژه در **مدل انتخاب مقصد (upstream)** است:

| موضوع | پروژه‌ی ما (فعلی) | SNISPF-HJ (مرجع) |
|---|---|---|
| تعداد IP مقصد | **یک** IP ثابت (`CONNECT_IP`) | **لیست** IP (`CONNECT_IPS`) |
| تعداد SNI جعلی | **یک** SNI ثابت (`FAKE_SNI`) | **لیست** SNI (`FAKE_SNIS`) |
| تست سلامت مسیر | ندارد (یک‌بار اسکن دستی) | **حلقه‌ی Health-Check خودکار هر ۳۰ ثانیه** |
| انتخاب مسیر برای هر اتصال | ثابت | **Weighted-Random** (هرچه packet-loss کمتر، شانس بیشتر) |
| جایگزینی مسیر خراب | دستی / restart | **Graceful Rotation** (اتصال‌های زنده قطع نمی‌شوند) |
| ماژول استخر اتصال | ندارد | `sni_spoofing/pool.py` |

**نتیجه:** ما یک «تک‌مسیر دستی» داریم؛ آن‌ها یک «استخر چندمسیره‌ی خودترمیم‌شونده» دارند.
این سند ۵ قابلیت اصلی + چند قابلیت فرعی را که باید پیاده کنیم، با جزئیات کامل توضیح می‌دهد.

> **توجه مهم درباره‌ی قابلیت‌های اصلی نسخه‌ی original (Fragment / Fake SNI / Combined /
> TTL Trick / Domain Checker):** بررسی نشان داد که **ما این‌ها را به شکل خودمان داریم**
> (`core/fragment.py`, `strategies/`, `transparent_spoof.py`, `core/cf_scanner.py`).
> پس آن‌ها «شکاف» نیستند؛ فقط باید هنگام پیاده‌سازی استخر، **حفظ شوند** (بخش ۶).
> شکاف واقعی، **استخر چند-IP/چند-SNI و چرخه‌ی سلامت آن** است (بخش‌های ۱ تا ۵).

---

## ۱. پشتیبانی از چندین IP و چندین SNI به‌صورت همزمان (Multi-IP / Multi-SNI Pool)

### وضعیت ما
در `config.json` فقط یک مقصد داریم:
```json
"CONNECT_IP": "104.19.229.21",
"FAKE_SNI": "www.hcaptcha.com",
```
`grep` در کل کد ما هیچ نشانی از `CONNECT_IPS`/`FAKE_SNIS`/استخر پیدا نکرد → **کاملاً غایب است**.

### آن‌ها چه دارند
فایل `sni_spoofing/config.json` آن‌ها دو **لیست** دارد (۱۱ IP × ۳۸ SNI = ۴۱۸ ترکیب):
```jsonc
"CONNECT_IPS": ["172.66.41.252", "108.162.196.145", "172.65.13.230", ...],
"FAKE_SNIS":   ["apple.com", "github.com", "google.com", "microsoft.com", ...]
```
ابزار **حاصل‌ضرب دکارتی** این دو لیست را می‌سازد (هر IP با هر SNI = یک «جفت / pair»)،
همه را تست می‌کند و خودش بهترین‌ها را انتخاب می‌کند.

### پیاده‌سازی مرجع (کجا نگاه کنیم)
- `sni_spoofing/pool.py` → تابع `build_connection_manager(config)`:
  - کلیدهای جدید جمع (`CONNECT_IPS`/`FAKE_SNIS`) را می‌خواند؛ اگر نبود به کلیدهای قدیمی
    تک‌مقداری (`CONNECT_IP`/`FAKE_SNI`) برمی‌گردد → **سازگاری کامل با حالت تک‌مسیره**.
  - اگر فقط یک جفت بماند، `None` برمی‌گرداند تا برنامه در همان مسیر «تک‌مقصد» قدیمی بماند
    (بدون سربار thread پس‌زمینه).
- `cli.py` خطوط ~۵۷۵–۵۸۷: ساخت `conn_manager` و انتخاب «IP نماینده» برای raw injector.

### نقشه‌ی پیاده‌سازی برای ما (استپ‌به‌استپ)
1. در `core/config_store.py` (یا هرجا config خوانده می‌شود) پشتیبانی از کلیدهای
   `CONNECT_IPS: list[str]` و `FAKE_SNIS: list[str]` اضافه شود؛ با fallback به
   `CONNECT_IP`/`FAKE_SNI` تک‌مقداری (شکستن سازگاری ممنوع).
2. یک ماژول جدید `core/pool.py` بسازیم (معادل `sni_spoofing/pool.py`).
3. تابعی شبیه `build_connection_manager(config)` که در حالت تک‌جفت `None` برگرداند تا
   هیچ تغییری در رفتار فعلی کاربران تک‌مسیره ایجاد نشود.
4. UI: یک تب/کارت «استخر مسیرها» که لیست IPها و SNIها را نشان دهد و وضعیت زنده‌ی هر جفت
   (سالم/ضعیف/مرده) را نمایش بدهد.

---

## ۲. Health Check خودکار (هر ۳۰ ثانیه)

### وضعیت ما
ما `core/cf_scanner.py` و `core/prober.py` را داریم که **اسکن دستی و یک‌باره** انجام می‌دهند
(کاربر دکمه می‌زند، اسکن می‌شود، تمام). هیچ **حلقه‌ی پس‌زمینه‌ی مداوم** نداریم که در حین کار
سلامت مسیر را پایش کند و خودکار مسیر خراب را عوض کند → **غایب است**.

### آن‌ها چه دارند
یک **thread دیمن پس‌زمینه** که هر `HEALTH_CHECK_INTERVAL` (پیش‌فرض ۳۰ ثانیه + jitter) همه‌ی
جفت‌های فعال و نمونه‌ای از جفت‌های ناشناخته را با **پروب TCP-connect** تست می‌کند.

### پیاده‌سازی مرجع
در `sni_spoofing/pool.py`:

- **کلاس `PairStats`** — آمار هر جفت `(IP, SNI)`:
  - `probe_loss_rate` — نرخ شکست پروب‌های TCP.
  - `real_loss_rate` — نرخ شکست در ترافیک واقعی کاربر.
  - `combined_loss_rate` — اگر `real_packets_sent > 10` آنگاه
    `0.7 * real_loss_rate + 0.3 * probe_loss_rate`؛ وگرنه فقط probe loss.
    (ترافیک واقعی مهم‌تر از پروب مصنوعی است → وزن ۰٫۷).
  - `score` — `inf` اگر جفت مرده باشد، `0.5` اگر هنوز پروب نشده، وگرنه `combined_loss_rate`.
  - `record_probe(success, dead_threshold=0.80)` و `record_real_packet(lost)`.
  - `MIN_PROBES = 3` — حداقل پروب لازم قبل از قضاوت.

- **کلاس `CombinationExplorer`** — کاشف تدریجی ترکیب‌ها:
  - `INITIAL_SAMPLE = 20` — در شروع فقط ۲۰ جفت تصادفی پروب می‌شود (نه همه‌ی ۴۱۸‌تا → سریع بالا می‌آید).
  - `EXPLORE_BATCH = 10` — هر دور، ۱۰ جفت ناشناخته‌ی جدید کشف می‌شود.
  - `VERIFY_TOP = 15` — ۱۵ جفت برتر مجدداً تأیید می‌شوند.
  - `_probe_one` پروب TCP-connect واقعی، `_run_probes_parallel` موازی.
  - `initial_explore` / `periodic_explore` / `stable_stats` / `known_stats` / `print_summary`.

- **کلاس `ConnectionManager`** — هماهنگ‌کننده:
  - `run_health_loop`: ابتدا `initial_explore` → `initialize` استخر → سپس حلقه‌ی بی‌نهایت:
    هر `interval + jitter` ثانیه → `periodic_explore` + `pool.refresh()`.
  - `start_health_loop`: حلقه را در یک thread دیمن اجرا می‌کند.

- **کلیدهای config مربوطه:**
  ```
  HEALTH_CHECK_INTERVAL = 30   # ثانیه بین هر دور
  HEALTH_CHECK_TIMEOUT  = 3    # timeout هر پروب TCP
  PROBE_COUNT           = 5    # تعداد پروب در هر دور برای هر جفت
  ACTIVE_SLOTS          = 3    # تعداد جفت‌های گرم نگه‌داشته‌شده
  LOSS_THRESHOLD        = 0.20 # بالاتر از این → جفت drain می‌شود
  DEAD_THRESHOLD        = 0.80 # بالاتر از این → جفت «مرده» علامت می‌خورد
  ```

### نقشه‌ی پیاده‌سازی برای ما
1. `PairStats` را در `core/pool.py` پیاده کنیم (دقیقاً همین فرمول `0.7/0.3`).
2. `CombinationExplorer` با همان ثابت‌ها (۲۰/۱۰/۱۵) — می‌توانیم پروب TCP را از `core/prober.py`
   موجود قرض بگیریم تا کد تکراری نشود.
3. حلقه‌ی سلامت را در یک `threading.Thread(daemon=True)` اجرا کنیم (مثل آن‌ها).
4. اتصال به UI: سیگنال زنده برای نمایش «آخرین Health-Check: X ثانیه پیش» و رنگ هر جفت.
5. کلیدهای config بالا را با همان پیش‌فرض‌ها به `config.json` و `core/config_store.py` بیفزاییم.

---

## ۳. انتخاب هوشمند مسیر — Weighted-Random

### وضعیت ما
نداریم — مقصد ثابت است؛ مفهوم «انتخاب وزنی» در کد ما وجود ندارد → **غایب است**.

### آن‌ها چه دارند
به‌جای انتخاب تصادفی یا round-robin، هر اتصال ورودی به یک جفت اختصاص می‌یابد با احتمالی که
**معکوس نرخ packet-loss** است: هرچه loss کمتر، شانس انتخاب بیشتر.

### پیاده‌سازی مرجع (در `ActivePool.pick()` داخل `pool.py`)
```python
weights = [1.0 / (ps.combined_loss_rate + 0.01) for ps in pool]
chosen  = random.choices(pool, weights=weights, k=1)[0]
```
- جمله‌ی `+ 0.01` از تقسیم‌بر‌صفر جلوگیری می‌کند و سقف وزن را محدود می‌کند.
- چون **تصادفی وزنی** است (نه فقط «بهترین»)، بار روی چند مسیر سالم پخش می‌شود و یک مسیر
  بیش‌ازحد داغ نمی‌شود (load-balancing طبیعی) و الگوی ترافیک قابل‌پیش‌بینی برای DPI نمی‌سازد.

### نقشه‌ی پیاده‌سازی برای ما
1. متد `pick()` در `ActivePool` ما با همین فرمول وزن‌دهی.
2. در forwarder/engine، هنگام برقراری هر اتصال جدید، `pick()` صدا زده شود تا (IP, SNI) آن اتصال
   تعیین شود.
3. تست واحد: با mock کردن `random.choices` مطمئن شویم وزن جفتِ کم‌loss بیشتر است.

---

## ۴. Graceful Rotation (چرخش بدون قطعی)

### وضعیت ما
نداریم — تغییر مقصد در ما یعنی restart اتصال یا اعمال دستی، که اتصال‌های فعال را قطع می‌کند
→ **غایب است**.

### آن‌ها چه دارند
وقتی یک جفت ضعیف می‌شود (loss > `LOSS_THRESHOLD`)، **فوراً قطع نمی‌شود**:
1. جفت ضعیف از استخر فعال خارج و به لیست `_draining` منتقل می‌شود.
2. **اتصال‌های فعال روی آن جفت تا پایان کارشان ادامه می‌یابند** (no new ones assigned).
3. جفت تنها وقتی کاملاً آزاد می‌شود که `active_connections == 0` شود.
4. جای خالی استخر با یک جفت سالم‌تر (از طریق Weighted-Random) پر می‌شود.

### پیاده‌سازی مرجع
- در `ActivePool`:
  - `slots` (تعداد جفت‌های فعال = `ACTIVE_SLOTS`)، لیست `_draining`.
  - `refresh()`: جفت‌های ضعیف → `_draining`؛ پر کردن جای خالی با weighted-random.
  - `report_failure()`: ثبت شکست واقعی روی یک جفت.
- در `forwarder.py`:
  - `handle_connection` از `pick_pair()` جفت می‌گیرد، موفقیت/شکست بسته‌ی واقعی را ثبت می‌کند،
    و در پایان `_release_pair()` را صدا می‌زند (شمارش `active_connections` را کم می‌کند).
  - موفقیت فقط **بعد از اولین پاسخ سرور (S→C)** ثبت می‌شود (تا اتصال‌های نیمه‌کاره مثبت کاذب نسازند).

### نقشه‌ی پیاده‌سازی برای ما
1. شمارنده‌ی `active_connections` per-pair در `PairStats` یا `ActivePool`.
2. لیست `_draining` و منطق آزادسازی با `active_connections == 0`.
3. در engine/forwarder ما (`core/engine.py` / لایه‌ی forwarder)، هنگام بستن هر اتصال،
   آزادسازی جفت را صدا بزنیم.
4. این مهم‌ترین قابلیت از نظر «تجربه‌ی کاربر» است — بدون آن، چرخش مسیر = قطعی لحظه‌ای.

---

## ۵. ردیابی شکست و Failover سریع (ConnectionTracker)

### وضعیت ما
ما `core/resilience.py` داریم (بودجه‌ی RST و throttle) ولی **ردیابی شکست per-IP با پنجره‌ی
زمانی برای failover خودکار** نداریم → **نیمه‌غایب / قابل‌تقویت**.

### آن‌ها چه دارند (در `forwarder.py`)
- کلاس `ConnectionTracker`:
  - `FAILOVER_THRESHOLD = 3` — اگر یک IP در پنجره‌ی زمانی ۳ بار پشت‌سرهم شکست بخورد، failover.
  - `FAILOVER_WINDOW = 30.0` — پنجره‌ی ۳۰ ثانیه‌ای برای شمارش شکست‌ها.
- `start_server`:
  - `MAX_CONCURRENT_CONNECTIONS = 512` با یک **semaphore** (محافظت در برابر اشباع).
  - `_raise_fd_limit` برای macOS (بالا بردن سقف file-descriptor).
  - پارامتر `conn_manager` برای یکپارچه‌سازی استخر.

### نقشه‌ی پیاده‌سازی برای ما
1. `ConnectionTracker` per-IP با همان آستانه‌ی ۳/پنجره‌ی ۳۰ ثانیه.
2. semaphore محدودکننده‌ی اتصال هم‌زمان در forwarder ما.
3. ادغام با `resilience.py` موجود به‌جای دوباره‌کاری.

---

## ۶. قابلیت‌های original که باید **حفظ** شوند (نه شکاف، اما حیاتی)

این‌ها در هر دو پروژه هستند و **نباید** هنگام افزودن استخر خراب شوند. معادل ما در پرانتز:

| قابلیت | در مرجع | معادل در پروژه‌ی ما |
|---|---|---|
| **Fragment** (sni_split / half / multi / tls_record_frag) | `bypass/fragment.py` + `tls/fragment.py` | `core/fragment.py` (TCP-seg + TLS-record) ✅ |
| **Fake SNI** (out-of-window seq trick) | `bypass/fake_sni.py` + `bypass/raw_injector.py` | `transparent_spoof.py` + `injecter.py` + `fake_tcp.py` ✅ |
| **Combined** (fake + fragment باهم) | `bypass/combined.py` | `strategies/` (موتور چندتکنیکی ما) ✅ |
| **TTL Trick** (fake با IP_TTL پایین روی سوکت جدا) | `_ttl_trick_and_fragment` در `fake_sni.py`/`combined.py` | باید بررسی شود؛ احتمالاً در `strategies/` (`fake_ttl`) ✅ |
| **Domain Checker** (تشخیص دامنه‌های پشت Cloudflare) | `scanner/domain_checker.py` | `core/cf_scanner.py` (پورت SenPaiScanner) ✅ |

### نکات ظریف مرجع که ارزش وام‌گرفتن دارند
1. **`_find_sni_offset`** در `tls/fragment.py`: تشخیص دقیق مرز SNI داخل ClientHello با
   اعتبارسنجی (`name_type == 0`, طول معقول، بایت‌های چاپ‌پذیر) — اگر فرگمنت ما این اعتبارسنجی
   را ندارد، اضافه کنیم تا fallback به «half-split» هوشمندتر شود.
2. **TTL Trick روی سوکت جداگانه**: نکته‌ی کلیدی این است که fake را روی **یک سوکت TCP مجزا**
   با `IP_TTL ∈ {1,2,3}` می‌فرستند تا به DPI برسد ولی قبل از سرور بمیرد — بدون آلوده‌کردن
   استریم اصلی TLS. (مدل تمیزتر از تزریق روی همان استریم.)
3. **`fragment_real=True` پیش‌فرض** در `fake_sni`: حتی وقتی raw injection کار می‌کند، باز هم
   ClientHello واقعی را در مرز SNI فرگمنت می‌کنند تا DPIهایی که TCP reassembly می‌کنند را هم
   دور بزنند (مخصوصاً کانفیگ‌های xhttp/ws با ALPN چندمقداری مثل `h3,h2,http/1.1`).
4. **Domain Checker**: رنج‌های رسمی Cloudflare inline (بدون وابستگی)، تشخیص ASN
   (`13335, 209242`)، تست TCP→TLS→HTTP و خروجی مرتب‌شده بر اساس latency و قابلیت usable_as_sni.
   اگر `cf_scanner.py` ما خروجی «لیست SNI آماده» نمی‌دهد، متد `export_sni_list` آن‌ها را الگو بگیریم.

---

## ۶.۵. وضعیت پیاده‌سازی (Progress)

> به‌روزرسانی: ۲۰۲۶-۰۶-۰۲ — **فاز ۱ + ۲ + ۳ کامل شد** (همه‌ی استپ‌های ۷.۱ تا ۷.۱۰).

| استپ | قابلیت | وضعیت | فایل |
|---|---|---|---|
| ۷.۱ | خواندن `CONNECT_IPS`/`FAKE_SNIS` با fallback | ✅ انجام شد | `core/config_store.py` (`connect_ips`/`fake_snis`/`pool_enabled`) + `config.json` |
| ۷.۲ | `PairStats` + فرمول combined-loss `0.7/0.3` | ✅ انجام شد | `core/pool.py` |
| ۷.۳ | `CombinationExplorer` (پروب TCP تزریق‌پذیر) | ✅ انجام شد | `core/pool.py` (`probe_fn` injectable برای تست headless) |
| ۷.۴ | `ActivePool` + Weighted-Random `pick()` (`1/(loss+0.01)`) | ✅ انجام شد | `core/pool.py` |
| ۷.۵ | `ConnectionManager` + حلقه‌ی Health-Check دیمن + `build_connection_manager` | ✅ انجام شد | `core/pool.py` (حلقه با `Event` قابل‌توقف + jitter) |
| ۷.۶ | Graceful Rotation (`_draining` + شمارش اتصال فعال) | ✅ انجام شد | `core/pool.py` (`acquire`/`release`/`refresh`) — متصل به forwarder در ۷.۷ |
| ۷.۷ | یکپارچه‌سازی forwarder/engine (`pick_pair` per-connection + ثبت موفقیت/شکست واقعی) | ✅ انجام شد | `main.py` (`ProxyServer._handle` انتخاب جفت per-connection + بازخورد نتیجه) + `core/engine.py` (`_build_pool`/`_stop_pool` + اتصال `conn_manager` به اسپوفر و توقف در `stop()`) |
| ۷.۸ | `ConnectionTracker` (failover per-IP ۳/۳۰s) | ✅ انجام شد | `core/pool.py` (`ConnectionTracker` + `FAILOVER_THRESHOLD=3`/`FAILOVER_WINDOW=30`؛ `pick_pair` از IPهای trip‌شده عبور می‌کند، `report_failure/success` ردیاب را تغذیه می‌کنند) |
| ۷.۹ | UI: کارت «استخر مسیرها» + ورودی لیست IP/SNI در تنظیمات | ✅ انجام شد | `ui/window.py` (`PoolPage` + تب «استخر» + ورودی `CONNECT_IPS`/`FAKE_SNIS` در `SettingsPage`) + `ui/engine_bridge.py` (`pool_summary`) + خط «بازیابی خودکار» و دکمه‌ی خروجی SNI |
| ۷.۱۰ | تقویت Domain Checker (`export_sni_list`) | ✅ انجام شد | `core/pool.py` (`export_sni_list` + `export_routes`) + دکمه‌ی «خروجی فهرست SNI…» در `PoolPage` |

**تست‌ها:** `tests/test_pool.py` (۴۷ تست) + `tests/test_pool_ui.py` (۲۳ تست) +
`tests/test_engine.py::EnginePoolIntegrationTest` (۵ تست) + افزوده‌ها در
`tests/test_config_store.py` و `tests/test_window_fixes_ui.py` —
**کل ۶۳۸ تست سبز، ۰ خطا، ۳ skip.**

**UI:** تب «استخر» در نوار کناری (`PoolPage`) وضعیت زنده‌ی هر مسیر (سالم/ضعیف/مرده،
افت، اتصال‌های فعال، آخرین سلامت‌سنجی) را نشان می‌دهد، به‌علاوه خط «بازیابی خودکار»
که IPهای موقتاً کنارگذاشته‌شده (failover) را فهرست می‌کند، و دکمه‌ی «خروجی فهرست SNI…»
برای ذخیره‌ی SNIهای فعلی در فایل متنی. در «تنظیمات» دو کادر چندخطی برای لیست IP/SNI
با راهنمای زنده‌ی «چند مسیر ساخته می‌شود» وجود دارد.

**یکپارچه‌سازی forwarder (۷.۷/۷.۸):** هر اتصال ورودی در `ProxyServer._handle` یک جفت
`(IP, SNI)` از استخر می‌گیرد (انتخاب وزنی، با عبور از IPهای در حالت failover)، نتیجه‌ی
واقعی (دانلود برگشتی = موفق، اتصال ناموفق/فقط-آپلود = شکست) به استخر و ردیاب بازخورد
داده می‌شود تا مسیرهای ضعیف به‌آرامی drain شوند. موتور حلقه‌ی سلامت‌سنجی را در `Start`
راه می‌اندازد و در `stop()` متوقف می‌کند.

**سازگاری تک‌مسیره:** حفظ شد — `build_connection_manager` در حالت تک‌جفت `None` برمی‌گرداند و
هیچ thread پس‌زمینه‌ای ساخته نمی‌شود؛ `conn_manager` در اسپوفر `None` می‌ماند و مسیر مستقیم قدیمی کار می‌کند.

---

## ۷. اولویت‌بندی و ترتیب پیشنهادی پیاده‌سازی (Roadmap)

| استپ | قابلیت | فایل‌های جدید/تغییری ما | پیش‌نیاز | ریسک |
|---|---|---|---|---|
| **۷.۱** | خواندن `CONNECT_IPS`/`FAKE_SNIS` با fallback | `core/config_store.py`, `config.json` | — | کم |
| **۷.۲** | `PairStats` + فرمول combined-loss | `core/pool.py` (جدید) | ۷.۱ | کم |
| **۷.۳** | `CombinationExplorer` (پروب TCP، با استفاده‌ی مجدد از `prober.py`) | `core/pool.py` | ۷.۲ | متوسط |
| **۷.۴** | `ActivePool` + Weighted-Random `pick()` | `core/pool.py` | ۷.۲ | کم |
| **۷.۵** | `ConnectionManager` + حلقه‌ی Health-Check (thread دیمن ۳۰s) | `core/pool.py` | ۷.۳، ۷.۴ | متوسط |
| **۷.۶** | Graceful Rotation (`_draining` + شمارش اتصال فعال) | `core/pool.py`, لایه‌ی forwarder | ۷.۵ | **بالا** |
| **۷.۷** | یکپارچه‌سازی forwarder/engine: `pick_pair` per-connection + ثبت موفقیت/شکست واقعی | `core/engine.py` | ۷.۶ | بالا |
| **۷.۸** | `ConnectionTracker` (failover per-IP ۳/۳۰s) + semaphore | لایه‌ی forwarder, `core/resilience.py` | ۷.۷ | متوسط |
| **۷.۹** | UI: کارت «استخر مسیرها» (وضعیت زنده‌ی هر جفت، آخرین health-check) | `ui/window.py` و یک ویجت جدید | ۷.۵ | متوسط |
| **۷.۱۰** | تقویت Domain Checker: خروجی لیست SNI قابل‌استفاده (`export_sni_list`) | `core/cf_scanner.py` | — | کم |

### اصول طلایی هنگام پیاده‌سازی
- **حالت تک‌مسیره نباید بشکند:** اگر فقط یک IP و یک SNI تعریف شده، استخر غیرفعال بماند
  (مثل `build_connection_manager` که `None` برمی‌گرداند) — بدون thread پس‌زمینه، بدون سربار.
- **شبکه injectable بماند:** مثل `core/cf_scanner.py`/`core/prober.py` فعلی ما، پروب را به‌صورت
  callable تزریق کنیم تا تست headless بدون سوکت واقعی ممکن باشد.
- **هر استپ یک commit + تست:** طبق رویه‌ی پروژه (genspark_ai_developer → PR).
- **ترافیک واقعی > پروب مصنوعی:** فرمول `0.7*real + 0.3*probe` را حفظ کنیم.

---

## ۸. نقشه‌ی فایل‌های مرجع (برای رجوع سریع در جلسات بعد)

کلون مرجع: `/home/user/SNISPF-HJ-ref/` (در صورت نبود، دوباره از
`https://github.com/hjfisher/SNISPF-HJ` کلون شود.)

| فایل مرجع | خطوط | چه چیزی آنجاست |
|---|---|---|
| `sni_spoofing/pool.py` | ~۶۳۵ | **قلب قابلیت جدید**: PairStats, CombinationExplorer, ActivePool, ConnectionManager, build_connection_manager |
| `sni_spoofing/forwarder.py` | ~۴۳۳ | forwarder asyncio + ادغام استخر، ConnectionTracker, handle_connection, start_server |
| `sni_spoofing/cli.py` | ~۶۹۵ | argparse، ساخت conn_manager، ساخت strategy، راه‌اندازی health-loop |
| `sni_spoofing/bypass/fragment.py` | ~۸۹ | استراتژی فرگمنت |
| `sni_spoofing/bypass/fake_sni.py` | ~۲۹۶ | fake SNI + TTL trick روی سوکت جدا + fragment_real |
| `sni_spoofing/bypass/combined.py` | ~۱۴۳ | fake + fragment باهم |
| `sni_spoofing/bypass/raw_injector.py` | ~۴۲۵ | seq_id trick با AF_PACKET (Linux/root) |
| `sni_spoofing/tls/fragment.py` | ~۱۶۰ | `_find_sni_offset`، چهار استراتژی فرگمنت |
| `sni_spoofing/scanner/domain_checker.py` | ~۴۳۰ | چکر دامنه‌ی Cloudflare (DNS→ASN→TCP→TLS→HTTP)، export_sni_list |
| `sni_spoofing/utils/__init__.py` | ~۱۳۰ | تشخیص پلتفرم، کشف interface، اعتبارسنجی IP/port |
| `config.json` | — | لیست ۱۱ IP × ۳۸ SNI + کلیدهای استخر |

---

## ۹. جمع‌بندی نهایی

**ما کم داریم (شکاف واقعی):**
1. ✅ استخر چند-IP/چند-SNI (`CONNECT_IPS`/`FAKE_SNIS` + حاصل‌ضرب دکارتی)
2. ✅ Health-Check خودکار پس‌زمینه هر ۳۰ ثانیه (PairStats + CombinationExplorer + ConnectionManager)
3. ✅ انتخاب مسیر Weighted-Random (`1/(loss+0.01)`)
4. ✅ Graceful Rotation (لیست `_draining` + شمارش اتصال فعال)
5. ✅ ConnectionTracker failover per-IP + semaphore محدودکننده

**ما داریم و فقط باید حفظ شوند:** Fragment، Fake SNI، Combined، TTL Trick، Domain Checker.

با پیاده‌سازی استپ‌های بخش ۷، پروژه‌ی ما از یک «تک‌مسیر دستی» به یک «استخر چندمسیره‌ی
خودترمیم‌شونده» ارتقا می‌یابد — دقیقاً همان جهشی که SNISPF-HJ نسبت به نسخه‌ی original کرد.

---

## ۱۰. بازطراحی (Phase 4) — استخر به‌عنوان «بهینه‌ساز پس‌زمینه»

**انگیزه:** در Phase 3 استخر منبعِ *اصلی* انتخاب مسیر شد (`pick_pair()` در هر اتصال)،
که مسیر تکیِ تأییدشده را با جفت‌های تصادفیِ سرد override می‌کرد → سیل `TimeoutError`.
کاربر گزارش داد نسخه‌ی اصلی «اول تست می‌کند بعد سریع می‌شود» ولی مال ما «یهو خوب یهو خراب».

**معماری جدید (مثل Make-Before-Break / Happy Eyeballs):**
1. **اول با مسیر تکیِ تأییدشده وصل می‌شویم** — بدون تأخیر. استخر دیگر هرگز per-connection
   مسیر را عوض نمی‌کند؛ هر اتصال از `connect_ip`/`fake_sni` فعلی استفاده می‌کند.
2. **استخر فقط در پس‌زمینه تست می‌کند** (حلقه‌ی سلامت)؛ ترافیک زنده دست نمی‌خورد.
3. **swap بدون قطع:** `ProxyServer.apply_route(ip, sni)` فقط مسیرِ اتصال‌های *جدید* را عوض
   می‌کند (اتصال‌های در جریان مسیر خود را حفظ می‌کنند). promoter در engine آن را صدا می‌زند.
4. **promoter دو-حالته** (`ConnectionManager.find_better_route`):
   - مسیر فعلی سالم → فقط با برتری قطعی (`PROMOTE_MARGIN=0.15`) ارتقاء (محافظه‌کارانه).
   - مسیر فعلی خراب → اولین مسیر سالم را فوراً جایگزین کن (اضطراری)، بعد ارتقاء ادامه دارد.
5. **ذخیره‌ی per-config:** بهترین (IP، SNI) در `POOL_BEST_RESULTS` با کلیدِ `config_identity`
   ذخیره و دفعه‌ی بعد به‌عنوان مسیر پیش‌فرض بارگذاری می‌شود (دیگر از صفر نمی‌گردیم).
6. **چک‌باکس opt-in:** `POOL_OPTIMIZE_ENABLED` (پیش‌فرض روشن). خاموش = فقط مسیر تکیِ ثابت،
   هیچ تستی انجام نمی‌شود.

**فایل‌های تغییر یافته:** `core/config_store.py` (کلیدها + helperها + deepcopy defaults)،
`main.py` (apply_route/current_route + حذف override)، `core/pool.py`
(best_candidate/find_better_route/lookup_pair + PROMOTE_MARGIN)، `core/engine.py`
(promoter + per-config best + gating)، `ui/window.py` (چک‌باکس + نمایش مسیر فعال/بهترین)،
`ui/engine_bridge.py` (active_route/best_route در snapshot).

**تست:** ۶۶۶ تست سبز (۲۸ تست جدید).

---

## ۱۱. اصلاحیه (Phase 4.1) — promote فقط بر اساس «ترافیک واقعی»، نه probe

**گزارش میدانی:** کاربر نسخه‌ی Phase 4 (commit `ca06d3d`) را تست کرد. «یه لحظه سرعت
خوبی داشت» ولی دوباره به سیل `TimeoutError` خورد. لاگ نشان داد:

* `20:12:42` مسیرِ `104.19.229.21 + www.hcaptcha.com` **کار کرد** (HTTP 200، محتوای واقعی).
* `20:13:16` promoter آن را با `172.66.41.252 + www.cloudflare.com` عوض کرد →
  بلافاصله سیل `TimeoutError`. سپس هر ~۱۰ ثانیه churn به google.com، phpbb.com،
  nextjs.org، apple.com، one.one.one.one … .

**ریشه‌ی واقعی (درس کلیدی):** یک probe تمیزِ TCP **اثبات نمی‌کند** که آن مسیر می‌تواند
یک ClientHello جعلی را از DPI رد کند. خیلی از IPهای CDN، TCP-وصل می‌شوند (probe loss=0)
ولی تزریق SNI جعلی تایم‌اوت می‌خورد. پس promote کردن صرفاً بر اساس probe loss، یک مسیرِ
*کارا* را با مسیرهای probe-سالم-ولی-DPI-مرده عوض می‌کرد. ضمناً مسیر تکیِ کاربر در استخرِ
۴۲۹تایی نبود؛ پس `lookup_pair`=None و successهای واقعیِ آن هرگز ثبت نمی‌شد.

**اصلاح (governed by REAL TRAFFIC):**
1. **قانون طلایی:** مسیرِ سالم (که ترافیک واقعی را موفق حمل می‌کند) **هرگز** عوض نمی‌شود.
   `find_better_route(..., current_healthy=True)` همیشه `None` برمی‌گرداند → پایانِ churn.
2. **فقط وقتی مسیر واقعاً خراب است** (failover tracker از شکست‌های واقعی فعال شده) swap
   می‌کنیم — و آن هم به **بهترین** کاندید که با ترافیک واقعی اثبات شده (`real_proven`).
3. **`PairStats.real_proven`:** مسیر فقط وقتی «اثبات‌شده» است که حداقل
   `REAL_PROOF_MIN_PACKETS` بسته‌ی واقعی با loss ≤ `REAL_PROOF_MAX_LOSS` حمل کرده باشد.
   `best_candidate()` کاندیدهای real-proven را همیشه بالاتر از probe-only رتبه می‌دهد.
4. **`ConnectionManager.ensure_pair(ip, sni)`:** برای مسیر تکیِ کاربر (که معمولاً در استخر
   نیست) یک `PairStats` می‌سازد تا successهای واقعیِ آن ثبت شوند و tracker بفهمد کار می‌کند.

**فایل‌های تغییر یافته:** `core/pool.py` (real_proven + best_candidate بر پایه‌ی proof +
find_better_route قانون طلایی + ensure_pair + REAL_PROOF_* constants)،
`core/engine.py` (بایند مسیر اولیه با `ensure_pair` به‌جای `lookup_pair`).

**تست:** ۶۷۰ تست سبز (۴ تست جدید: قانون طلایی، ترجیح real-proven، ensure_pair، real_proven).

---

## ۱۲. اصلاحیه (Phase 4.2) — probeِ «دست‌دادنِ جعلی» = اطمینانِ کامل قبل از سوئیچ

**گزارش میدانی دوم:** کاربر Phase 4.1 را تست کرد. «اولش کار کرد ولی بعدش نه». لاگ
(`20:38`–`20:43`) دو چیز را نشان داد:

* مسیر اولیه `104.19.229.21 + www.hcaptcha.com` در تستِ داخلی HTTP 200 داد، ولی به
  محض شروعِ ترافیک واقعیِ YouTube (ده‌ها اتصال همزمان) سیل `TimeoutError` آمد و
  failover درست فعال شد.
* بعد promoter هر ~۱۰ ثانیه به مسیرهای جدید churn کرد (sciencedirect.com، rust-lang.org،
  microsoft.com، vercel.com …) که **همه** TimeoutError دادند. کاربر گفت IP/دامینِ
  تأییدشده‌ی خودش را هم به لیست اضافه کرده بود و «عجیب است که هیچ‌کدام کار نکردند».

**ریشه‌ی واقعی:** استخر فقط **TCP-connect** را probe می‌کرد. یک TCP-connect تمیز
اثبات نمی‌کند که SNI جعلی از DPI رد می‌شود — به همین خاطر «همه‌چیز probe-سالم بود ولی
هیچ‌چیز کار نمی‌کرد». ضمناً هیچ مسیری هرگز `real_proven` نمی‌شد (چون ترافیک واقعی فقط روی
مسیر فعال ثبت می‌شد)، پس promoter یا churn می‌کرد یا هدفِ اثبات‌شده‌ای نداشت.

**اصلاح (probeِ high-confidence):**
1. **`spoof_handshake_probe(ip, port, timeout, fake_sni)`** — به‌جای TCP-connect خام،
   دقیقاً همان ClientHello با **SNI جعلی** که اسپوفر زنده می‌فرستد را replay می‌کند و
   منتظر پاسخِ TLS سرور می‌ماند:
   * سرور بایت TLS برمی‌گرداند → decoy از DPI رد شد → مسیر **تأییدشده** است.
   * RST/بسته‌شدن یا سکوت (timeout) → DPI آن SNI جعلی را کشت → مسیر **مرده** است.
   این probe **بدون WinDivert و بدون Admin** کار می‌کند (socketِ مستقلِ خودش) و هرگز به
   مسیر داده‌ی زنده (xray↔spoofer)، پینگ‌گرفتن یا کانفیگ‌های معمولی دست نمی‌زند.
2. **`PairStats.spoof_proven` + `record_spoof_probe`:** یک مسیر با عبورِ موفقِ
   دست‌دادنِ جعلی `real_proven` می‌شود — *قبل* از هر ترافیک واقعی. probeِ شکست‌خورده
   مسیر را زنده فرض نمی‌کند.
3. **دروازه‌ی اطمینان در `find_better_route`:** حتی وقتی مسیر فعلی خراب است، swap
   **فقط** به مسیری انجام می‌شود که `real_proven` باشد (با ترافیک واقعی یا با
   دست‌دادنِ جعلیِ تأییدشده). اگر هیچ مسیر اثبات‌شده‌ای نباشد، روی مسیر فعلی می‌ماند به‌جای
   churn روی مسیرهای ناآزموده. این دقیقاً خواسته‌ی کاربر است: «بدون اطمینانِ کامل، سوئیچ
   نشود».
4. **پیش‌فرضِ runtime:** وقتی نه `probe_fn` و نه `spoof_probe_fn` داده شود (یعنی
   runtime واقعی)، `ConnectionManager` خودکار `spoof_handshake_probe` را وصل می‌کند.
   تست‌هایی که `probe_fn` تزریق می‌کنند سمانتیکِ TCP خام را حفظ می‌کنند.

**فایل‌های تغییر یافته:** `core/pool.py` (spoof_handshake_probe + SpoofProbeFn +
spoof_proven/record_spoof_probe + explorer/manager/factory wiring + confidence gate)،
`main.py` و `core/engine.py` (پیام‌های لاگ شفاف‌تر).

**تست:** ۶۴ تست `test_pool.py` سبز (۷ تست جدید برای spoof-probe) + ۱۵۴ تست
pool/engine/forwarder بدون شکست. (شکست‌های PySide6 در sandbox بی‌ربط‌اند.)

---

## ۱۳. اصلاحیه (Phase 4.3) — توقفِ churn و رفعِ سیلِ TimeoutError / wsarecv

لاگِ ۲۱:۳۳–۲۱:۳۷ نشان داد Phase 4.2 «کمی پایدارتر» شد ولی هنوز خطا می‌داد:
سیلِ «دست‌دادن جعلی شکست/تایم‌اوت (TimeoutError)» و خطای جدیدِ
`wsarecv: An existing connection was forcibly closed` روی پورت 40443، و
churnِ ادامه‌دار (xbox→nodejs→apple→deepseek→gmail هر چند ثانیه).

**ریشه‌یابی (تأییدشده با خواندنِ کد + لاگ):**
1. **هر دو خطا یک رویداد‌اند.** هر `wsarecv: forcibly closed` روی 40443 دقیقاً همان
   لحظه‌ای است که `_handle` پنج ثانیه منتظرِ تأییدِ ACKِ ClientHelloِ جعلی از
   WinDivert می‌ماند (`t2a_event`)، تأیید نمی‌رسد، تابع return می‌کند و در `finally`
   سوکتِ ورودیِ xray بسته می‌شود → xray می‌بیند اتصالش «به‌زور» بسته شد. یعنی خرابی
   در **لایه‌ی تزریقِ WinDivert** است، نه در لایه‌ی مسیر — عوض‌کردنِ مسیر هرگز آن را
   درست نمی‌کند، فقط churn و شلوغیِ لاگ می‌سازد.
2. **`spoof_handshake_probe` یک false-positive است.** این probe یک socketِ **مستقیم**
   به IPِ CDN باز می‌کند و ClientHelloِ جعلی می‌فرستد؛ لبه‌ی Cloudflare به تقریباً
   *هر* ClientHello روی :443 پاسخِ TLS می‌دهد → probe برای تقریباً هر IPِ در دسترس
   پاس می‌شود. بنابراین ده‌ها مسیرِ مرده «تأییدشده» جلوه می‌کردند و promoter بینشان
   churn می‌کرد، در حالی‌که همه در لایه‌ی تزریقِ زنده شکست می‌خوردند.

**رفع:**
1. **فقط ترافیک واقعی مسیر را اثبات می‌کند.** `real_proven` دیگر به `spoof_proven`
   تکیه نمی‌کند؛ تنها معیارِ promotion، عبورِ موفقِ **ترافیک واقعیِ فوروارد‌شده** روی
   همان مسیر است. `spoof_proven` صرفاً به‌عنوان hintِ زنده‌بودن / tie-break در
   رتبه‌بندی باقی می‌ماند (`_candidate_is_better`) و **به‌تنهایی هرگز** اجازه‌ی سوئیچ
   نمی‌دهد. نتیجه: وقتی لایه‌ی تزریق خراب است، هیچ مسیری `real_proven` نمی‌شود →
   `find_better_route` همیشه `None` برمی‌گرداند → **churn متوقف می‌شود** و برنامه روی
   مسیرِ انتخابی/ذخیره‌شده‌ی کاربر می‌ماند تا خطای واقعیِ WinDivert/Admin شفاف دیده شود.
2. **cooldownِ ضدِ flap در promoter:** پس از هر سوئیچ، تا یک پنجره‌ی settling
   (حداقل ۳۰ ثانیه) سوئیچِ بعدی بررسی نمی‌شود تا مسیرِ جدید فرصتِ اثباتِ خود با ترافیک
   واقعی را داشته باشد — حتی اگر کاندیدای اثبات‌شده‌ای پیدا شود.

**عدمِ تخریبِ عملکردِ معمول:** هیچ تغییری در مسیرِ داده‌ی زنده، پینگ‌گرفتن، یا
کانفیگ‌های معمولی داده نشد؛ تعدادِ thread‌های probe دست‌نخورده ماند.

**فایل‌های تغییر یافته:** `core/pool.py` (`real_proven` فقط ترافیک واقعی،
`_candidate_is_better` با tie-breakِ spoof)، `core/engine.py` (cooldownِ ضدِ flap +
پیامِ لاگِ شفاف‌تر)، `main.py` (پیامِ لاگِ سوئیچ)، `tests/test_pool.py`.

**تست:** ۶۷ تست `test_pool.py` سبز + ۱۵۶ تست pool/engine/forwarder بدون شکست.
