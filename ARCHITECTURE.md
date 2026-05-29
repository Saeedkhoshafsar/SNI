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
- `ed25519.py` — **وریفایر Ed25519 خالص پایتون (فقط verify، RFC 8032)** بدون وابستگی؛ برای صحت‌سنجی امضای `strategies.json`.
- `strategies_remote.py` — **کانال آپدیت امضاشده** (استپ ۱۱): `Recipe`/`Manifest`/`StrategiesUpdater`؛ fetch از mirrorها + verify امضا + نگاشت به `Candidate` پرابر. شبکه‌تزریق‌پذیر (fetcher بیرونی).
- `diagnostics.py` — **لایه‌ی diagnostics** (استپ ۱۲، داده‌ی خالص): `snapshot(engine)` → `DiagnosticsSnapshot` (استراتژی فعال، سلامت کاندیداها، throughput/throttle، شمارش RST، زنجیره‌ها). بدون Qt؛ صفحه‌ی UI فقط poll می‌کند.
- `config_store.py` — **ConfigStore**: persist کردن `config.json` + `profiles.json`، fail-soft. `DEFAULT_CONFIG` اینجاست.
- `admin.py` — **هلپر self-elevation** (استپ ۱۳، خالص و قابل‌تست): `is_windows/is_frozen/is_admin/relaunch_params/ensure_admin`. فقط فراخوانیِ واقعیِ `ShellExecuteW("runas")` ویندوز-اونلی است؛ منطقِ تصمیم (نیاز به elevate؟ ساخت argvِ relaunch) خالص و unit-tested.
- `warp_manager.py` / `vwarp_manager.py` / `binary_utils.py` — مدیریت باینری‌های warp/vwarp و کمک‌باینری. `binary_utils.get_bin_dir()` در حالت frozen از `sys._MEIPASS/bin` می‌خواند (هماهنگ با `--add-data "bin;bin"` در spec).

### Packaging / entrypoint (استپ ۱۳)
- `app.py` (ریشه) — **entrypointِ نازکِ exe**: ابتدا `core.admin.ensure_admin(argv)` (اگر relaunch شد، خروج)، سپس `ui.app_qt.main(theme=...)`. فلگِ `--theme {light,dark}`.
- `SNISpoofer.spec` — اسپکِ PyInstaller (onefile، `console=False`، مپِ `bin/`→`bin/`، WinDivert از pydivert با `collect_data_files`+`collect_dynamic_libs`، آیکون، hidden-importها، excludeهای حجمی).
- `scripts/build_exe.py` — هلپرِ build با preflight + پاک‌سازی + اجرای PyInstaller (روی Linux import-safe، فقط روی ویندوز build می‌کند).
- `scripts/make_icon.py` + `assets/app.ico` — آیکونِ چندسایزه (سپرِ تیره + گلیفِ «S» سایان نئونی + رینگِ بنفش).
- `requirements-build.txt` — وابستگی‌های build (`pyinstaller`, `pillow`)؛ جدا از requirements اصلیِ runtime.

### UI (`ui/`) — PySide6
- `app_qt.py` — entrypointِ UI (`python -m ui.app_qt`)؛ RTL، high-DPI، ساخت MainWindow. (در حالت packaged از طریق `app.py` فراخوانی می‌شود.)
- `window.py` — **MainWindow** + ۶ صفحه (Dashboard/Profiles/Settings/Strategy/**Diagnostics**/Log). `DiagnosticsPage` یک renderer نازک است که `engine.diagnostics()` را روی QTimer (فقط هنگام نمایش) poll می‌کند. لیست `STRATEGIES` و `MODES` اینجاست.
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

### قدیمی/آرشیو
- `legacy/gui.py`, `legacy/gui_old2.py` — UIِ tkinter قدیمی (بازنشسته، فقط مرجعِ تاریخی). هیچ کدِ فعالی به آن‌ها وابسته نیست.

### CI / build خودکار
- `ci/release.yml` (کاربر به `.github/workflows/` می‌بَرَد) — روی ویندوزِ GitHub Actions با `SNISpoofer.spec` exe می‌سازد. هر push/PR → **Artifact**؛ tag `v*` → **Release**. نیازی به secret دستی نیست (`GITHUB_TOKEN` خودکار).
- `scripts/download_bins.py` — دانلودِ fallbackِ xray/vwarp اگر در repo نبودند.
- `BUILD.md` — راهنمای کاملِ فارسیِ استخراجِ exe (از GitHub Actions یا build دستی روی ویندوز).

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

## ۶.۶) به‌روزرسانی امضاشده از راه دور (استپ ۱۱ — anti-dictation) ✅

وقتی سانسور یک ترفند را در سطح ملی بکوبد، باید بتوان ترفند جدید را با آپدیت یک فایل **داده**
(نه کد) پخش کرد، بدون انتشار build جدید:
- `strategies.json` آرایه‌ای از **رسپی‌های اعلانی** است: کلید استراتژیِ شناخته‌شده + پارامترهای
  fragmentation. چون کد اجرایی ندارد، mirror آلوده نمی‌تواند چیزی اجرا کند.
- maintainer فایل را با کلید **خصوصیِ آفلاین** Ed25519 امضا می‌کند؛ اپ کلید **عمومی** متناظر را
  embed دارد و هر fetch را با `core/ed25519.py` (خالص پایتون، بدون وابستگی) verify می‌کند.
  فایل امضانشده/دستکاری‌شده رد می‌شود و مجموعه‌ی خوب قبلی حفظ می‌ماند (fail-closed روی trust).
- از چند **mirror** (GitHub raw، IPFS، endpoint پشت همان bypass) کشیده می‌شود؛ اولین mirror با
  payloadِ معتبر و `version` جدیدتر برنده است.
- `StrategiesUpdater` رسپی‌های فعال را به `Candidate` پرابر نگاشت می‌کند و engine آن‌ها را به‌جای
  رجیستری محلی به Auto-Prober می‌دهد. کل منطق load/verify/merge داده‌ی خالص و آزمون‌پذیر است؛
  ابزار آفلاینِ امضا در `tools/sign_strategies.py`.

---

## ۶.۷) صفحه‌ی تشخیص (استپ ۱۲ — شفافیتِ زنده) ✅

Auto-Prober و تاب‌آوری حالا حالت داخلیِ غنی نگه می‌دارند؛ کاربر باید بتواند آن را ببیند بدون
اینکه GUI به internalها کوپل شود:
- `core/diagnostics.snapshot(engine)` یک `DiagnosticsSnapshot` داده‌ی خالص می‌سازد: استراتژی فعال،
  سلامت اندازه‌گیری‌شده‌ی هر کاندیدا (score/success/samples/outcome)، throughput و verdictِ throttle،
  شمارش RST جعلی و زنجیره‌های fallback. کاملاً tolerant (idle/نبود prober → پیش‌فرض، نه crash).
- `DiagnosticsPage` در UI فقط این snapshot را روی QTimer (هر ۱ ثانیه، فقط هنگام نمایشِ صفحه) poll و
  render می‌کند: کارت خلاصه + نوار throughput + جدول کاندیداها با نشانگر ★ برای انتخاب‌شده. تست
  headless آف‌اسکرین با skip مودبانه در نبود Qt.

---

## ۶.۸) بسته‌بندی و self-elevation (استپ ۱۳ — تحویلِ نهایی) ✅

محصول باید به یک **exe تک‌فایلیِ ویندوزی** تبدیل شود که کاربر بدون نصبِ پایتون اجرا کند:
- `app.py` (entrypointِ exe) → `core.admin.ensure_admin(argv)` → در صورت نیاز با `ShellExecuteW(..,"runas",..)`
  خودش را **با دسترسی Administrator** دوباره اجرا می‌کند (WinDivert به admin نیاز دارد) و پروسه‌ی غیر-elevate خارج می‌شود؛
  در غیر این صورت `ui.app_qt.main(theme)` را بالا می‌آورد. منطقِ تصمیمِ elevation **خالص و unit-tested** است (`core/admin.py`).
- `SNISpoofer.spec` همه‌ی `bin/` را به `bin/` داخلِ `_MEIPASS` مپ می‌کند (هماهنگ با `binary_utils.get_bin_dir()`) و
  درایور/کتابخانه‌ی **WinDivert** را از pydivert جمع می‌کند؛ بدون کنسول، با آیکونِ `assets/app.ico`.
- `scripts/build_exe.py` با preflight (ویندوز؟ PyInstaller نصب؟ `bin/` کامل؟) build را امن می‌کند؛ روی Linux فقط import-safe است.
- اصلِ معماری حفظ می‌شود: هیچ منطقِ کاربردی در لایه‌ی بسته‌بندی نیست؛ صرفاً bootstrap + bundling.

---

## ۷) گام‌های باقی‌مانده (خلاصه — جزئیات در PLAN.md)

- **همه‌ی استپ‌های roadmap (۱…۱۳) تکمیل شدند.** ✅
- **نظافتِ پس از roadmap انجام شد:** tkinter قدیمی به `legacy/` آرشیو شد؛ workflowِ CI برای buildِ خودکارِ
  exe روی ویندوز بازنویسی شد (Artifact روی هر push، Release روی tag). تنها موردِ باقی‌مانده یک build/اجرای
  واقعی روی ماشینِ ویندوز برای تأییدِ نهایی است (sandbox فقط import/preflight را تست می‌کند) — که حالا با
  push کردن به GitHub به‌صورت خودکار توسط Actions انجام می‌شود.

---

## ۸) اجرا و تست (یادآوری عملی)

```bash
# تست‌ها (روی sandbox هم کار می‌کند چون وابستگی WinDivert/xray دارای fake است)
python3 -m pytest tests/ -q

# اجرای UI (روی ویندوزِ کاربر؛ روی sandbox فقط import تست می‌شود)
python -m ui.app_qt

# وابستگی‌ها
pip install -r requirements.txt   # pydivert + PySide6

# ساخت exe نهایی (فقط روی ویندوز)
pip install -r requirements-build.txt           # pyinstaller + pillow
python scripts/build_exe.py                      # → dist/SNISpoofer.exe
# یا مستقیم:  pyinstaller SNISpoofer.spec

# یا بدون ویندوزِ محلی: push به GitHub → تب Actions → دانلودِ Artifact
# یا  git tag v1.0.0 && git push origin v1.0.0  → ساختِ Release خودکار
```

نیازمندی‌های ویندوز: WinDivert (همراه pydivert) + اجرای با دسترسی administrator
(exeِ ساخته‌شده خودش از طریق `core/admin.ensure_admin` elevate می‌شود).
