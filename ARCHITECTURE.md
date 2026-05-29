# ARCHITECTURE.md — منطق کلی و معماری پروژه‌ی SNI Spoofer

> این فایل **مکمل `PLAN.md`** است. PLAN فقط *کارهای انجام‌شده* را قدم‌به‌قدم ثبت می‌کند؛
> این فایل **چرایی، منطق کلی، جریان داده و قراردادهای معماری** را شرح می‌دهد تا اگر بکاپ
> پروژه از دست رفت، بشود کلیت مسیر و طرز کار سیستم را بازسازی کرد.
> منبع استراتژیِ سطح‌بالا: `ROADMAP_DEAD_END_PROOF.md`. وضعیت گام‌ها: `PLAN.md`.

---

## ۱) هدف محصول (در یک نگاه)

ابزار **دور زدن DPI/فیلترینگ روی ویندوز** که SNI واقعی را با تزریق یک ClientHello «جعلی»
و/یا تکه‌تکه‌کردن ClientHello واقعی از دید DPI پنهان می‌کند. هدف نهایی:

1. **UI حرفه‌ای** (PySide6، حس گیمینگ×هکری، تم لایت/دارک، RTL فارسی).
2. **تجربه‌ی v2rayN-style:** کاربر فقط یک **share-link** یا **subscription URL** را paste می‌کند،
   یک دکمه می‌زند؛ برنامه خودش xray-core را بالا می‌آورد و **SNI-spoofing را خودکار زیر آن
   زنجیر می‌کند** — بدون هیچ هماهنگی دستی پورت (`127.0.0.1:40443`).
3. **Arsenal بن‌بست‌ناپذیر:** چند تکنیک bypass + لایه‌ی fragmentation + یک Auto-Prober که
   بهترین تکنیک را خودکار کشف و قفل می‌کند.

**محدوده:** فقط ویندوز (نیاز به WinDivert + دسترسی admin). اندروید/لینوکس فعلاً خارج از scope.

---

## ۲) ایده‌ی فنی هسته (چرا کار می‌کند)

DPIِ stateful وقتی یک TCP flow را می‌بیند، ClientHello و SNIِ داخل آن را بازرسی می‌کند.
دو خانواده تکنیک برای فریب آن داریم:

### الف) Fake injection (تزریق بسته‌ی جعلی) — پکیج `strategies/`
قبل از ارسال ClientHelloِ واقعی، یک ClientHelloِ **جعلی** (با SNIِ بی‌خطر مثل `www.speedtest.net`)
تزریق می‌شود که طوری دستکاری شده که **سرورِ واقعی آن را دور می‌ریزد ولی DPI آن را می‌پذیرد/ثبت می‌کند**.
بعد از آن، ClientHelloِ واقعی (با SNIِ تحریم‌شده) از DPIِ که حالا «گیج/desync شده» عبور می‌کند.
راه‌های «دورریختنِ سرور ولی پذیرشِ DPI»:

| تکنیک | کلید | مکانیزم |
|------|------|---------|
| Wrong Sequence | `wrong_seq` | seq خارج از پنجره → سرور به‌عنوان داده‌ی قدیمی drop می‌کند |
| Wrong Checksum | `wrong_checksum` | checksum نامعتبر → NIC/استک سرور drop، DPI بی‌توجه به checksum |
| Fake TTL | `fake_ttl` | TTL پایین → بسته در مسیر می‌میرد، فقط به DPI نزدیک می‌رسد |
| Multi Fake | `multi_fake` | تکرار چندباره‌ی fake برای DPIهایی که دیرتر lock می‌کنند |
| Fake Disorder | `fake_disorder` | دو کپی نامرتب برای مختل‌کردن بازچینی DPI |

### ب) Fragmentation (تکه‌کردن ClientHelloِ واقعی) — ماژول `core/fragment.py`
بدون هیچ بسته‌ی جعلی: خودِ ClientHelloِ واقعی را می‌شکنیم تا DPI نتواند SNI را به‌صورت یک
رشته‌ی پیوسته match کند:
- **TCP segmentation:** برش TCP **داخل خود رشته‌ی SNI** (DPI الگوی `\x00<host>` کامل را در یک
  segment نمی‌بیند). lossless است.
- **TLS record fragmentation:** یک TLS record را به چند record کوچک با هدر معتبر می‌شکنیم؛
  سرور بازچینی می‌کند ولی DPIِ ساده فقط record اول را می‌بیند.

این لایه **داده‌ی خالص و بدون pydivert** است → روی هر OS قابل تست/استفاده (مسیر آینده‌ی چندپلتفرمی).

---

## ۳) معماری ماژول‌ها و جریان داده

```
                  ┌──────────────────────────── UI (PySide6, ui/) ───────────────────────────┐
                  │  app_qt.py  →  window.MainWindow (5 صفحه)                                  │
                  │  Dashboard / Profiles / Settings / Strategy / Log                          │
                  │  theme.py · widgets.py · animations.py · win_effects.py                    │
                  └───────────────▲───────────────────────────────────┬───────────────────────┘
                  سیگنال‌های Qt    │ (log/status/count)                  │ start()/stop()/config
                                  │                                     ▼
                       ┌──────────┴───────────┐         ┌──────────────────────────────┐
                       │ ui/engine_bridge.py  │◀────────│      core/engine.py          │
                       │ EngineBridge(QObject)│ callback│   EngineController (هسته)     │
                       │ callback→Qt signal   │         │  UI-agnostic orchestration   │
                       └──────────────────────┘         └───────┬──────────────┬───────┘
                                                                 │              │
                            ┌────────────────────────────────────┘              │
                            ▼ (حالت v2rayN: زنجیر)                                ▼
                ┌───────────────────────────┐                       ┌───────────────────────────┐
                │   core/xray_manager.py     │  outbound →           │     main.ProxyServer       │
                │  xray-core (subprocess)    │  127.0.0.1:spoof_port │  (spoofer + injection)     │
                │  config: core/xray_config  │──────────────────────▶│  bypass_method, FAKE_SNI   │
                └───────────────────────────┘                       └───────────┬───────────────┘
                                                                                  │ per-connection
                                                                                  ▼
                                                       ┌────────────────────────────────────────┐
                                                       │ fake_tcp.FakeTcpInjector (WinDivert)     │
                                                       │  fake_send_thread → get_strategy(method) │
                                                       │  strategy.mutate_fake_packet / send_fake │
                                                       └──────────────────▲───────────────────────┘
                                                                          │ (registry)
                                                       ┌──────────────────┴───────────────────────┐
                                                       │ strategies/ (BypassStrategy registry)     │
                                                       │ wrong_seq · wrong_checksum · fake_ttl ·   │
                                                       │ multi_fake · fake_disorder                │
                                                       └───────────────────────────────────────────┘
```

### جریان «یک کلیک» (حالت v2rayN)
1. کاربر share-link را paste می‌کند → `core/share_link.parse_link` → `core/profile.Profile`.
2. `EngineController.start()`:
   - یک پورت آزاد loopback انتخاب می‌کند (`find_free_port`) → `spoof_port`.
   - `main.ProxyServer` را با `CONNECT_IP/PORT = آدرس واقعی سرور` بالا می‌آورد و `bypass_method` را ست می‌کند.
   - `core/xray_manager.XrayManager` را با outbound به `127.0.0.1:spoof_port` بالا می‌آورد
     (TLS/SNI همچنان سرور واقعی را توصیف می‌کند).
3. ترافیک: اپ کاربر → xray (socks/http) → spoofer (تزریق DPI-bypass) → سرور واقعی.

### جریان «SNI Only»
بدون xray؛ فقط `ProxyServer` به‌عنوان فورواردر خام با `CONNECT_IP/PORT` که کاربر می‌دهد.

---

## ۴) نقشه‌ی فایل‌ها (مرجع سریع)

### هسته (`core/`) — کاملاً UI-agnostic
- `engine.py` — **EngineController**: قلب orchestration. وضعیت‌ها: IDLE/CONNECTING/ACTIVE/ERROR.
  start/stop روی thread جدا، idempotent. callbackها: `on_log/on_status/on_count`.
- `profile.py` — **Profile** (dataclass چندپروتکلی): vless/vmess/trojan/ss + transport/security/...
- `share_link.py` — پارسر share-link و subscription (base64/url-decode/SIP002/...).
- `xray_config.py` — ساخت xray config (inbound socks/http + outbound + routing). `dest` قابل override برای زنجیر.
- `xray_manager.py` — **XrayManager**: مدیریت subprocess xray؛ با `spoof_port` به loopback وصل می‌شود.
- `fragment.py` — لایه‌ی fragmentation (داده‌ی خالص؛ بدون pydivert).
- `prober.py` — **Auto-Prober** (استپ ۹): `Candidate`/`ProbeResult`/`HealthWindow`/`AutoProber`؛ probe قابل تزریق، ranking/قفل/پایش زنده + `fallback_order()`.
- `resilience.py` — **لایه‌ی تاب‌آوری** (استپ ۱۰، داده‌ی خالص): `RstClassifier` (تشخیص RST جعلی)، `ThroughputMonitor` (تشخیص throttle)، `ResilienceController` (سیاست چرخش استراتژی/IP با تکیه بر `fallback_order` پرابر). بدون pydivert/Qt.
- `config_store.py` — **ConfigStore**: persist کردن `config.json` + `profiles.json`، fail-soft. `DEFAULT_CONFIG` اینجاست.
- `warp_manager.py` / `vwarp_manager.py` / `binary_utils.py` — مدیریت باینری‌های warp/vwarp و کمک‌باینری.

### UI (`ui/`) — PySide6
- `app_qt.py` — entrypoint (`python -m ui.app_qt`)؛ RTL، high-DPI، ساخت MainWindow.
- `window.py` — **MainWindow** + ۵ صفحه (Dashboard/Profiles/Settings/Strategy/Log). لیست `STRATEGIES` و `MODES` اینجاست.
- `engine_bridge.py` — **EngineBridge(QObject)**: callback هسته → سیگنال Qt (thread-safe، queued).
- `theme.py` — Palette لایت/دارک، `build_qss`, accentها (سایان نئونی `#27e0c8`، بنفش `#9b7bff`).
- `widgets.py` — Card, NavItem, TitleBar, **PowerButton** (idle/connecting/active/error), **Toast**.
- `animations.py` — fade/slide/stagger، **PulseDot**, **CountUp**, ColorTransition.
- `win_effects.py` — Mica/acrylic روی ویندوز (fallback مات).

### تزریق (سطح پایین — نیاز به WinDivert/admin، فقط ویندوز)
- `main.py` — **ProxyServer**: کلید config `LISTEN_HOST/PORT`, `CONNECT_IP/PORT`, `FAKE_SNI`؛
  صفت `bypass_method` (پیش‌فرض `wrong_seq`)؛ callbackهای `on_log/on_status_change/on_connection_count_change`.
- `fake_tcp.py` — **FakeInjectiveConnection** (صفات: `bypass_method, fake_data, syn_seq, fake_sent, monitor, thread_lock`)
  و **FakeTcpInjector** (`self.w` = WinDivert). متد `fake_send_thread` از registry استفاده می‌کند.
- `injecter.py` — کلاس پایه‌ی `TcpInjector` (WinDivert recv/inject loop).
- `monitor_connection.py` — `MonitorConnection` (state اتصال: syn_seq/syn_ack_seq/...).
- `transparent_spoof.py` — مسیر transparent (نسخه‌ی قدیمی‌تر forwarder با همان تکنیک fake-hello).
- `utils/packet_templates.py` — **ClientHelloMaker** (template ۵۱۷ بایتی TLS؛ مرجع همه‌ی fakeها).
  نکته: record-layer version در این CH مقدار `0x0301` است (نه `0x0303`).

### استراتژی‌ها (`strategies/`)
- `base.py` — `BypassStrategy` (هوک‌های `mutate_fake_packet`/`send_fake`/`score`)، `StrategyMeta`،
  رجیستری `REGISTRY` + `@register` + `get_strategy` + `all_strategies`، و هلپر مشترک `apply_fake_payload`.
- هر تکنیک یک فایل: `wrong_seq.py`, `wrong_checksum.py`, `fake_ttl.py`, `multi_fake.py`, `fake_disorder.py`.

### قدیمی/آرشیو (بعد از تثبیت UI جدید حذف می‌شوند)
- `gui.py`, `gui_old2.py` — UIِ tkinter قدیمی.

---

## ۵) قراردادها و الگوهای کلیدی (مهم برای ادامه‌ی کار)

1. **جدایی هسته از UI:** هسته فقط callback fire می‌کند؛ هیچ ارجاع Qt در `core/` نیست.
   UI از طریق `EngineBridge` callback را به سیگنال Qt (cross-thread queued) تبدیل می‌کند.

2. **رجیستری استراتژی self-registering:** افزودن یک فایل زیر `strategies/` + دکوریتور `@register`
   + import جانبی در `strategies/__init__.py` کافی است. **هیچ وابستگی سخت به pydivert در زمان import**
   نباشد (packet/connection داک-تایپ‌اند) تا روی sandbox تست شود.

3. **جریان `bypass_method`:** `config → engine._proxy.bypass_method → main.ProxyServer → FakeInjectiveConnection
   → fake_tcp.get_strategy(connection.bypass_method)`. کلید ناشناخته فقط لاگ می‌شود (پروسه kill نمی‌شود).

4. **هم‌خوانی UI ↔ REGISTRY:** هر کلید در `ui/window.STRATEGIES` باید یک پیاده‌سازی واقعی در `REGISTRY`
   داشته باشد (هیچ گزینه‌ی «coming soon»). یک تست/چک این را تضمین می‌کند.

5. **تست‌ها دوحالته:** هر فایل تست هم با `python3 tests/x.py` (self-runner با `sys.path.insert`)
   و هم با `pytest` سبز می‌شود. fakeها (ProxyServer/Xray/packet/connection) جایگزین وابستگی‌های
   غیرقابل‌اجرا روی sandbox (WinDivert/xray.exe) هستند. monkeypatchها در `tearDown` بازگردانده می‌شوند
   تا بین تست‌ها نشت نکنند.

6. **fragmentation lossless ≠ byte-identical:** TCP-segmentation بایت‌ها را عوض نمی‌کند (lossless کامل)؛
   اما TLS-record-fragmentation هدرهای record اضافه می‌کند → stream **بزرگ‌تر** می‌شود ولی *بدنه‌ی*
   handshake بدون تغییر بازچینی می‌شود.

7. **Workflow گیت (هر استپ):** کد → commit → `git fetch origin main` → `git rebase origin/main`
   → push به `genspark_ai_developer` → ساخت PR → اشتراک لینک PR → آپدیت `PLAN.md` → گزارش → انتظار «ادامه بده».
   کاربر معمولاً PR را خودش merge می‌کند؛ استپ بعد روی origin/main ربیس می‌شود.

---

## ۶) Auto-Prober (استپ ۹ — «غول آخر»، منطق هدف) ✅

هنگام Start، prober چند کاندیدا (استراتژی‌های inject × گزینه‌های fragmentation) را روی
`CONNECT_IP` تست می‌کند: آیا **ServerHello واقعی** برگشت؟ RST خورد؟ latency؟ سپس امتیاز می‌دهد،
**بهترین را انتخاب و قفل** می‌کند. با sliding-window سلامت را پایش می‌کند و در صورت افت، دوباره
probe و سوییچ خودکار می‌کند. `BypassStrategy.score()` فقط یک prior استاتیک برای **ترتیب اولیه‌ی**
کاندیداهاست؛ prober موفقیت واقعی را اندازه می‌گیرد. این قلب «بن‌بست‌ناپذیری» است.
خروجی `fallback_order()` پرابر، زنجیره‌ی تاب‌آوری استپ ۱۰ را تغذیه می‌کند.

---

## ۶.۵) تاب‌آوری (استپ ۱۰ — بقا در برابر سانسور فعال) ✅

پرابر «بهترینِ لحظه‌ی Start» را پیدا می‌کند؛ تاب‌آوری «بقا در حین جلسه» را تضمین می‌کند وقتی DPI
**فعالانه** حمله می‌کند (نه فقط مسدودسازی منفعل):
- **RST جعلی:** DPI یک RST تزریق می‌کند تا اتصال را قطع کند. `RstClassifier` با موقعیت/زمان
  (و در صورت وجود، TTL) آن را از RST واقعی تشخیص می‌دهد؛ `ResilienceController` تا سقف `rst_budget`
  آن را **نادیده می‌گیرد** (سبک zapret) و سپس استراتژی را می‌چرخاند.
- **Throttle:** DPI به‌جای قطع، سرعت را می‌خواباند. `ThroughputMonitor` با مقایسه‌ی نرخ اخیر با
  baseline افت پایدار را تشخیص می‌دهد → چرخش فوری استراتژی.
- **زنجیره‌ی fallback:** استراتژی‌ها تمام شد → چرخش `CONNECT_IP` از pool → همه تمام شد → `GIVE_UP`.
- engine هنگام Start کنترلر را می‌سازد، زنجیره‌ها را از `fallback_order` پرابر + `CONNECT_IP_ALTS`
  پر می‌کند و آن را به `ProxyServer.resilience` می‌دهد؛ خودِ drop/چرخش زنده در runtime ویندوز
  (pydivert) با مشورت همین کنترلر انجام می‌شود. کل منطق pure-data و روی sandbox تست‌شده است.

---

## ۷) گام‌های باقی‌مانده (خلاصه — جزئیات در PLAN.md)

- استپ ۱۱: `strategies.json` از راه دور (mirror + امضای ed25519، اعمال داغ).
- استپ ۱۲: صفحه‌ی Strategy/Diagnostics (استراتژی فعال، نمودار سلامت، probeها).
- استپ ۱۳: Packaging با PyInstaller (یک exe، embed باینری‌ها، آیکون).

---

## ۸) اجرا و تست (یادآوری عملی)

```bash
# تست‌ها (روی sandbox هم کار می‌کند چون وابستگی WinDivert/xray دارای fake است)
python3 -m pytest tests/ -q

# اجرای UI (روی ویندوزِ کاربر؛ روی sandbox فقط import تست می‌شود)
python -m ui.app_qt

# وابستگی‌ها
pip install -r requirements.txt   # pydivert + PySide6
```

نیازمندی‌های ویندوز: WinDivert (همراه pydivert) + اجرای با دسترسی administrator.
