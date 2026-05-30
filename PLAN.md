# PLAN.md — SNI Spoofer Windows: نسخه حرفه‌ای + Arsenal بن‌بست‌ناپذیر

هدف نهایی (نسخه‌ی **فقط ویندوز**):
1. **UI حرفه‌ای مدرن** — شیشه‌ای/مات، سایه‌پذیر، تم لایت/دارک، حرکات پویا (انیمیشن)،
   حس شفاف و رنگ‌های حرفه‌ای. نه یک فرم خشک‌وخالی.
2. **Arsenal چندلایه‌ی خودتطبیق** — «غول مرحله‌ی آخر» فیلترینگ که از هر بن‌بستی رها می‌کند:
   چندین تکنیک bypass + موتور auto-prober که خودکار بهترین را کشف و قفل می‌کند.

منابع مرجع (بررسی‌شده): patterniha/SNI-Spoofing (Python/WinDivert)، sni-spoof (Go)،
کد فعلی webapp (ProxyServer + TransparentSpoofServer + xray/vwarp + GUI tkinter).
سند استراتژی کامل: `ROADMAP_DEAD_END_PROOF.md`.

---

## 🎯 تصمیمات معماری (قطعی)

- **پلتفرم:** فقط Windows (نیاز به WinDivert + admin). اندروید/لینوکس حذف از scope فعلی.
- **UI:** مهاجرت از tkinter به **PySide6 (Qt6)**. حس بصری: **گیمینگ × هکری** —
  دارک مات عمیق، accent نئونی سایان، سایه‌ی مینیمال، بدج‌های مونواسپیس ترمینالی،
  تم لایت/دارک. tkinter قدیمی بازنشسته می‌شود (`gui.py`/`gui_old2.py`).
- **هسته‌ی bypass:** نگه‌داشتن `ProxyServer` + `TransparentSpoofServer` موجود، اما
  استخراج تکنیک تزریق به یک **StrategyEngine** ماژولار که چند تکنیک را پشتیبانی کند.
- **یکپارچه‌سازی هسته (سبک v2rayN):** یک نرم‌افزار، یک کلیک. کاربر فقط
  **share link** (`vless://`/`vmess://`/`trojan://`/`ss://`) یا **Subscription URL** وارد می‌کند؛
  برنامه خودش پارس می‌کند، xray config می‌سازد، و SNI-spoofing را **خودکار زیر
  همان اتصال زنجیر می‌کند** (forwarder روی `40443` داخلی/نامرئی — کاربر نمی‌بیند).
  پشتیبانی چندپروتکل (VLESS/VMess/Trojan/Shadowsocks) به‌جای فقط trojan.
- **هماهنگی:** هسته (engine/managers) کاملاً از UI جدا — UI فقط callback مصرف می‌کند.

---

## وضعیت کلی استپ‌ها

- [x] استپ ۱ — اسکلت UI حرفه‌ای PySide6 (تم لایت/دارک + شیشه‌ای/Mica + سایه + هدر/ناوبری)  ✅ 2026-05-29
- [x] استپ ۲ — اجزای پویا: کارت‌های انیمیشن‌دار، دکمه‌ی Start/Stop با حالت‌گذار نرم، toast/log زنده  ✅ 2026-05-29
- [x] استپ ۳ — **پارسر share-link + subscription** (vless/vmess/trojan/ss) → مدل پروفایل  ✅ 2026-05-29
- [x] استپ ۴ — **یکپارچه‌سازی هسته v2rayN-style** + زنجیر خودکار spoofing زیر اتصال (حذف 127.0.0.1:40443 دستی)  ✅ 2026-05-29
- [x] استپ ۵ — اتصال UI به هسته (پروفایل‌ها/start/stop/callbackها) + مدیریت config  ✅ 2026-05-29
- [x] استپ ۶ — StrategyEngine: استخراج interface تکنیک + ثبت تکنیک‌های موجود (wrong_seq)  ✅ 2026-05-29
- [x] استپ ۷ — افزودن تکنیک‌ها: wrong_checksum (فعال‌سازی)، fake_ttl، multi-fake، split/disorder  ✅ 2026-05-29
- [x] استپ ۸ — لایه‌ی fragmentation: TCP split + TLS record fragmentation (مستقل از موقعیت)  ✅ 2026-05-29
- [x] استپ ۹ — **غول آخر: Auto-Prober** — تست خودکار استراتژی‌ها، ranking، انتخاب/قفل خودکار  ✅ 2026-05-29
- [x] استپ ۱۰ — تاب‌آوری: تشخیص RST جعلی، throttle، چرخش CONNECT_IP/استراتژی، fallback chain  ✅ 2026-05-29
- [x] استپ ۱۱ — strategies.json از راه دور (mirror + امضا) برای آپدیت بدون انتشار اپ  ✅ 2026-05-29
- [x] استپ ۱۲ — صفحه‌ی Strategy/Diagnostics در UI (نمایش استراتژی فعال، نمودار سلامت، probeها)  ✅ 2026-05-29
- [x] استپ ۱۳ — Packaging: PyInstaller (یک exe)، آیکون، self-elevate admin، تست build، bundle نهایی  ✅ 2026-05-29

---

## 🎨 فاز ۲ — بازطراحی UX/UI (بازخورد کاربر پس از بیلد و تست موفق)

بازخورد کاربر (نسخه‌ی بیلدشده کار می‌کند ولی UI ناقص است):
1. لاگ‌ها کوتاه/ناگویا — بدون timestamp، بدون سطح/رنگ.
2. باید لینک کانفیگ paste شود → دیالوگ ادیت با فیلدهای پرشده باز شود → کاربر ادیت/تأیید کند (نه تنظیم دستی 127.0.0.1:40443).
3. کانفیگ فعال هیچ نشانه‌ای ندارد جز رنگ — باید برچسب «● فعال» + جزئیات.
4. پنجره از سربرگ درست drag نمی‌شود.
5. استراتژی‌ها کلیک‌پذیر نیستند — باید با کلیک انتخاب/قفل شوند.
6. تم شیشه‌ای ضعیف — حس مدرن/سه‌بعدی/مینیاتوری/معلق/سایه‌دار لازم است.
7. معلوم نیست tunnel است یا proxy — گزینه‌ی «پروکسی سیستم» لازم است.
8. پنل واحد نیست؛ مصرف آپلود/دانلود زنده نیست؛ ناهماهنگی استراتژی فعال بین داشبورد و تشخیص.

- [x] استپ ۱۴ — رفع ناهماهنگی استراتژی فعال (engine → UI callback) + نظافت پوشه‌ی خراب `‎.github` + شمارش بایت آپلود/دانلود در ProxyServer  ✅ 2026-05-29
- [x] استپ ۱۵ — دیالوگ ادیت پروفایل: paste لینک → پارس → فرم پرشده‌ی قابل‌ویرایش → افزودن/ذخیره (بازخورد ۲)  ✅ 2026-05-29
- [x] استپ ۱۶ — لیست پروفایل غنی: برچسب «● فعال»، جزئیات سرور (پروتکل/آدرس/transport/امنیت)، آیکون پروتکل، دکمه‌ی ویرایش (بازخورد ۳)  ✅ 2026-05-29
### 🆕 بازخورد ۹ (مهم — اضافه‌شده ۲۰۲۶-۰۵-۲۹): سنجش پینگ پیش از اتصال
کاربر: «قبل اتصال باید بدونم کدوم سرور پینگ پایین‌تر و حتی دانلود بهتری داره. وقتی پینگ می‌گیریم
گزینه‌ای باشه که استراتژی‌ها رو تست کنه ببینه با کدوم بهتر/قابل‌اتصاله، یا بشه استراتژی رو
انتخاب کرد که با اون پینگ بگیریم.» — این به دو استپ با اولویت بالا تبدیل شد (۱۷ و ۱۸، درج‌شده پیش از بقیه).

- [x] استپ ۱۷ — **هسته‌ی پینگ/لتنسی** (`core/ping.py`): سنجش پینگ TCP هر پروفایل، رتبه‌بندی «کدوم سرور پایین‌تر»، تخمین کیفیت دانلود (throughput سبک)، و **تست استراتژی هنگام پینگ** (کدوم استراتژی قابل‌اتصال/بهتر است) با امکان انتخاب استراتژی برای پینگ — UI-agnostic، شبکه‌تزریق‌پذیر، بازاستفاده از `core/prober.py` (بازخورد ۹)  ✅ 2026-05-29
- [x] استپ ۱۸ — **UI پینگ**: دکمه/پنل «پینگ همه» و «پینگ این پروفایل» در ProfilesPage (نمایش ms + بهترین سرور + کیفیت دانلود)، انتخابگر استراتژی برای پینگ، و نمایش نتیجه‌ی تست استراتژی‌ها (کدوم وصل می‌شود) پیش از اتصال (بازخورد ۹)  ✅ 2026-05-29
### 🆕 بازخورد ۱۰ (اضافه‌شده ۲۰۲۶-۰۵-۲۹، حین استپ ۱۹): اشتراک LAN برای گوشی
کاربر: «ببین LAN رو اضافه کردی؟ برای مواقعی که می‌خوام به گوشی شیر کنم لازم می‌شه.» — وضعیت قبلی:
xray فقط روی `127.0.0.1` گوش می‌داد، پس گوشی در شبکه‌ی محلی نمی‌توانست وصل شود. به‌عنوان استپ مجزای
اولویت‌بالا (۱۹) درج شد؛ داشبورد و بقیه یک شماره جلو رفتند.

- [x] استپ ۱۹ — **اشتراک LAN**: گزینه‌ی opt-in برای bind پروکسی socks/http روی `0.0.0.0`، نمایش آدرس IP محلی برای تنظیم در گوشی (هر دو روی یک Wi-Fi)، با هشدار امنیتی (بازخورد ۱۰)  ✅ 2026-05-29
- [x] استپ ۲۰ — داشبورد واحد «کنترل‌مرکز»: نمودار زنده‌ی آپلود/دانلود (Sparkline)، نرخ زنده ↓/↑، مصرف کل، استراتژی فعالِ زنده، نوار وضعیت resilience، و نشان tunnel/proxy (بازخورد ۸ و ۵b و ۷)  ✅ 2026-05-30
### 🆕🐞 بازخورد ۱۱ (باگ بحرانی — اضافه‌شده ۲۰۲۶-۰۵-۳۰، حین استپ ۲۰): کانفیگ‌های افزوده‌شده کار نمی‌کنند
کاربر: «کانفیگ‌های اضافه‌شده داخل نرم‌افزار کار نمی‌کنند؛ مجبورم با V2RayTun وصل شوم. قبلاً فکر می‌کردم
درست کار می‌کند چون حواسم نبود V2RayTun در پس‌زمینه روشن بود.» نمونه‌ی کانفیگ: VLESS + **XHTTP** روی
Cloudflare Worker (`type=xhttp`, `mode=auto`). علت ریشه‌ای: `_stream_settings` فقط
`tcp/ws/grpc/http/h2/quic/kcp` را می‌شناخت و `xhttp` را بی‌صدا به `tcp` تنزل می‌داد → دست‌دادِ غلط →
عدم اتصال. این یعنی ادعای «بی‌نیازی از v2rayN» عملاً برای ترنسپورت مدرن XHTTP نقض شده بود.

- [x] استپ ۲۱ — **رفع باگ XHTTP** (بحرانی): افزودن `xhttp`/`splithttp`/`httpupgrade` به `TRANSPORTS`، فیلد `mode` به `Profile`، گرفتن `mode=` در پارسر (vless/trojan/vmess)، ساخت درست `xhttpSettings`/`httpupgradeSettings` در `_stream_settings` (با نرمال‌سازی `splithttp→xhttp`)، و نمایش/ویرایش `mode` در دیالوگ پروفایل (بازخورد ۱۱)  ✅ 2026-05-30
- [x] استپ ۲۲ — پروکسی سیستم ویندوز (set/unset رجیستری) + سوییچ در UI «تونل/پروکسی سیستم» (بازخورد ۷)  ✅ 2026-05-30
- [x] استپ ۲۳ — لاگ حرفه‌ای: timestamp، سطح‌بندی info/ok/warn/err با رنگ، فیلتر، شمارنده (بازخورد ۱)  ✅ 2026-05-30
- [x] استپ ۲۴ — صفحه‌ی استراتژی کلیک‌پذیر + بازطراحی تم سه‌بعدی/مدرن (کارت‌های معلق/شیشه/سایه عمیق/هاور) + رفع drag پنجره (بازخورد ۴/۵/۶)  ✅ 2026-05-30

---

## شرح استپ‌ها (هرکدام ۴–۶ کار)

### استپ ۱ — اسکلت UI حرفه‌ای PySide6
1. افزودن PySide6 به requirements + ساخت پکیج `ui/` با entrypoint `app_qt.py`.
2. پنجره‌ی اصلی frameless با گوشه‌ی گرد + پس‌زمینه‌ی شیشه‌ای (Mica/acrylic روی Win11، fallback مات).
3. سیستم تم: توکن‌های رنگ لایت/دارک + سوییچ تم با ذخیره در config.
4. هدر سفارشی (drag + دکمه‌های min/close سفارشی) + ناوبری کناری/تب‌ها (Dashboard/Settings/Strategy/Log).
5. لایه‌ی سایه (drop-shadow) روی کارت‌ها + استایل‌شیت QSS مرکزی.
6. اجرای آزمایشی headless-safe (تست import روی sandbox؛ نمایش واقعی روی ویندوز کاربر).

### استپ ۲ — اجزای پویا
کارت‌های متحرک (fade/slide در ورود)، دکمه‌ی Start/Stop با ripple/گذار رنگ، نوار وضعیت
زنده، toastهای لاگ، اسپینر/نبض هنگام probe. انیمیشن با `QPropertyAnimation`.

### استپ ۳ — پارسر share-link + subscription
پارس `vless://`/`vmess://`(base64 json)/`trojan://`/`ss://` به یک مدل پروفایل
یکپارچه، دکد base64، URL-decode، پارس query params (sni/host/path/type/security/fp…)،
واکشی سابسکریپشن (base64 چندخطی) → لیست پروفایل‌ها. تست unit روی نمونه‌ها.

### استپ ۴ — یکپارچه‌سازی هسته (v2rayN-style)
تعمیم `XrayManager` به همه‌ی پروتکل‌ها (vless/vmess/trojan/ss outbound، reality/ws/grpc/tcp)،
ساخت خودکار outbound از پروفایل، و **زنجیر خودکار spoofing**: فورواردر داخلی
روی پورت loopback با server_address و server_port پروفایل تزریق می‌شود — کاربر هیچ پورتی
دستی وارد نمی‌کند. یک کلیک = spoof + core با هم.

### استپ ۵ — اتصال UI ↔ هسته
اتصال callbackهای on_log/on_status/on_count به سیگنال‌های Qt (thread-safe)، لیست
پروفایل‌ها + دکمه‌ی import-from-clipboard + start/stop، بارگذاری/ذخیره config و پروفایل‌ها.

### استپ ۶ — StrategyEngine (اسکلت)
interface `BypassStrategy` (متد `apply(packet/conn)` + `probe()` + متادیتا)، رجیستری
استراتژی‌ها، refactor تزریق فعلی به استراتژی `WrongSeqStrategy`.

### استپ ۷ — تکنیک‌های injection
فعال‌سازی `wrong_checksum` (کد کامنت‌شده)، `fake_ttl` (TTL پایین برای fake)، `multi_fake`
(چند SNI whitelisted)، `fake_disorder` (تزریق + ارسال نامرتب). هرکدام یک Strategy.

### استپ ۸ — fragmentation
TCP segmentation (split ClientHello روی SNI) + TLS record fragmentation. قابل ترکیب با
fake. این لایه حتی بدون injection هم کار می‌کند (مسیر آینده‌ی چندپلتفرمی).

### استپ ۹ — غول آخر: Auto-Prober
موتوری که هنگام Start، چند استراتژی را موازی روی CONNECT_IP تست می‌کند (آیا ServerHello
واقعی برگشت؟ RST؟ latency؟)، امتیاز می‌دهد، بهترین را انتخاب و قفل می‌کند؛ با sliding-window
سلامت را پایش می‌کند و در صورت افت، دوباره probe و سوییچ خودکار. → بن‌بست‌ناپذیری.

### استپ ۱۰ — تاب‌آوری
تشخیص RST جعلی (drop کردنش)، تشخیص throttle (افت throughput)، چرخش خودکار
CONNECT_IP از pool + سوییچ استراتژی، زنجیره‌ی fallback.

### استپ ۱۱ — strategies.json از راه دور
فرمت JSON استراتژی‌ها، fetch از چند mirror، امضای ed25519، اعمال داغ بدون restart.

### استپ ۱۲ — صفحه‌ی Strategy/Diagnostics
نمایش استراتژی فعال + امتیاز هرکدام + لاگ probe + نمودار throughput/latency زنده.

### استپ ۱۳ — Packaging
PyInstaller (onefile)، embed باینری‌ها (xray/vwarp/wintun)، آیکون، تست، bundle.

---

## 🐛 باگ‌های یافته‌شده
- [استپ ۱] آیکن‌های emoji رنگی در sandbox رندر نشدند (فونت emoji نصب نیست) — با گلیف‌های یونیکد geometric جایگزین شد که در فونت پایه هستند؛ روی ویندوز Segoe UI درست نمایش داده می‌شوند. حل‌شده در همین استپ، نیازی به استپ مجزا نیست.
- [استپ ۲] تداخل دو graphics-effect: کارت دارای drop-shadow هنگام ورود، opacity-effect دوم می‌گرفت (خطای QPainter "not active"). — `_opacity_effect` اصلاح شد تا اگر ویجت از قبل eff﻿ect غیر-opacity دارد، fade را skip و فقط slide کند. حل‌شده در همین استپ.
- [استپ ۵] monkeypatch ماژول‌سطح در `test_engine` (جایگزینی `XrayManager`/`main`) به `test_xray_config` نشت می‌کرد چون pytest همه را در یک پروسه اجرا می‌کند. — `_install_fakes` اکنون یک callable برای بازگردانی برمی‌گرداند و `tearDown` آن را صدا می‌زند تا تست‌ها ایزوله بمانند. حل‌شده در همین استپ.

## ✅ استپ ۵ — جزئیات پیاده‌سازی (2026-05-29)
- `core/config_store.py` — `ConfigStore`: بارگذاری/ذخیره‌ی `config.json` + `profiles.json` (لیست پروفایل + ایندکس انتخاب‌شده)، fail-soft روی فایل خراب/غایب.
- `core/engine.py` — `EngineController`: قلب orchestration و UI-agnostic. یک‌کلیک v2rayN: انتخاب پورت آزاد loopback برای spoofer، اجرای `ProxyServer` با تزریق، و زنجیر خودکار `XrayManager` پشت آن (outbound → 127.0.0.1:spoof_port). در حالت «SNI Only» فقط فورواردر خام. start/stop روی thread جدا، idempotent.
- `ui/engine_bridge.py` — `EngineBridge(QObject)`: callbackهای thread-affine موتور را به سیگنال‌های Qt (`log/status/count`) تبدیل می‌کند تا اتصال به ویجت‌ها روی thread اصلی GUI امن باشد.
- `ui/window.py` — صفحه‌ی جدید `ProfilesPage` (import share-link/subscription، paste از clipboard، لیست + حذف/انتخاب). اتصال Dashboard (دکمه‌ی Power → start/stop واقعی، شمارش زنده، status)، Settings (load/save در ConfigStore)، Log (append زنده، bounded buffer). ذخیره‌ی تم. `closeEvent` موتور را تمیز متوقف می‌کند.
- تست‌ها: `tests/test_config_store.py` (۹) + `tests/test_engine.py` (۶) — مجموعاً ۳۷ تست سبز (با fakeها برای ProxyServer/XrayManager چون WinDivert/xray.exe در sandbox نیستند).

## ✅ استپ ۶ — جزئیات پیاده‌سازی (2026-05-29)
- پکیج جدید `strategies/` — موتور ماژولار bypass، **بدون وابستگی سخت به pydivert در زمان import** تا روی هر OS (بدون WinDivert) قابل enumerate/تست/نمایش در UI باشد.
- `strategies/base.py` — `StrategyMeta` (dataclass فریز‌شده: key/title/description/implemented/tags)، کلاس پایه‌ی `BypassStrategy` با هوک‌های `mutate_fake_packet` (abstract)، `send_fake` (پیش‌فرض: یک ارسال تزریقی با recalc)، `score` (پیش‌فرض ۰٫۵)؛ رجیستری `REGISTRY` + دکوریتور `@register` (نمونه‌سازی و ثبت با meta.key، خطای ValueError روی کلید تکراری) + `get_strategy` (KeyError با پیام فارسی شامل کلیدهای موجود) + `all_strategies(implemented_only=...)`.
- `strategies/wrong_seq.py` — `WrongSeqStrategy`: استخراج عینی تکنیک قدیمی (psh + افزودن fake payload + bump ipv4.ident + `seq_num = (syn_seq + 1 - len(payload)) & 0xffffffff`)، `score()=0.8` (تا قبل از یادگیری prober اولویت اول).
- `strategies/__init__.py` — API عمومی + import جانبی `wrong_seq` برای self-register.
- `fake_tcp.py` — `fake_send_thread` بازنویسی شد: به‌جای `if bypass_method == "wrong_seq" … else sys.exit("not implemented")`، حالا از رجیستری استفاده می‌کند: `strategy = get_strategy(connection.bypass_method); strategy.mutate_fake_packet(...); strategy.send_fake(...)`. خطای کلید ناشناخته به‌جای کشتن پروسه فقط لاگ می‌شود. (`import sys` همچنان لازم است — در `inject()` برای "impossible direction!".)
- تست‌ها: `tests/test_strategies.py` (۲۰ تست) با fakeهای duck-typed برای packet/connection/injector — رجیستری، KeyError، all_strategies، متادیتا/score، فرمول wrong_seq + wrap امضانشده ۳۲بیتی، bump و wrap ۱۶بیتی ipv4.ident، مسیر IPv6 (ipv4=None)، و رفتار `send_fake`. مجموعاً **۵۷ تست سبز** (هم direct-run هم pytest).

## ✅ استپ ۷ — جزئیات پیاده‌سازی (2026-05-29)
- **هلپر مشترک** در `strategies/base.py`: متد استاتیک `apply_fake_payload(packet, connection)` که پرولوگ مشترک همه‌ی تکنیک‌های fake را اجرا می‌کند (psh + افزودن fake payload + رشد packet_len + bump ipv4.ident). `wrong_seq` هم بازآرایی شد تا از همین هلپر استفاده کند (رفتار دست‌نخورده).
- **`strategies/wrong_checksum.py`** — `WrongChecksumStrategy`: seq داخل پنجره + checksum عمداً نامعتبر (`0x0000`)؛ سرور بسته را دور می‌ریزد ولی DPIِ بی‌توجه به checksum، SNI جعلی را ثبت می‌کند. **بحرانی:** `send_fake` با `recalc=False` ارسال می‌کند تا WinDivert checksum را «تعمیر» نکند. `score()=0.6`.
- **`strategies/fake_ttl.py`** — `FakeTTLStrategy`: TTL پایین (پیش‌فرض ۴، قابل override با `connection.fake_ttl`) تا بسته فقط به DPI برسد نه سرور؛ مسیر IPv6 از `ipv6.hop_limit` استفاده می‌کند. `score()=0.55`.
- **`strategies/multi_fake.py`** — `MultiFakeStrategy`: همان mutation wrong_seq + ارسال چندباره (پیش‌فرض ۳، قابل override با `connection.fake_repeat`) در `send_fake`. `score()=0.65`.
- **`strategies/fake_disorder.py`** — `FakeDisorderStrategy`: دو کپی نامرتب با seqهای متفاوت (هر دو خارج از پنجره) برای مختل‌کردن بازچینی DPI. `score()=0.6`.
- **`strategies/__init__.py`** — هر چهار تکنیک جدید side-effect import شدند تا self-register شوند. حالا REGISTRY شامل ۵ استراتژی است.
- **هم‌خوانی با UI:** `STRATEGIES` در `ui/window.py` (که از قبل هر ۵ کلید را لیست کرده بود) حالا کاملاً با پیاده‌سازی‌های واقعی پشتیبانی می‌شود — هیچ منوی «coming soon» باقی نمانده.
- تست‌ها: `tests/test_strategies.py` گسترش یافت (۳۵ تست) — fakeها حالا `checksum`/`ttl`/`ipv6.hop_limit` دارند؛ تست متادیتا/score هر تکنیک، corrupt-checksum + ارسال recalc=False، TTL/hop_limit + override، تعداد ارسال multi_fake + override، دو کپی fake_disorder با seq متفاوت، و شبیه‌سازی ترتیب dispatch واقعی. مجموعاً **۷۲ تست سبز**.

## ✅ استپ ۸ — جزئیات پیاده‌سازی (2026-05-29)
- **`core/fragment.py`** — لایه‌ی fragmentation به‌صورت **داده‌ی خالص، بدون pydivert/OS** (کاملاً قابل تست روی sandbox و مسیر آینده‌ی چندپلتفرمی):
  - پارسر هدر TLS record: `parse_record_header` (type/version/body_len)، `is_tls_handshake`.
  - **یافتن SNI:** `find_sni_offset` ساختار کامل ClientHello را می‌پیماید (record → handshake → random → session_id → cipher_suites → compression → extensions → server_name) و آفست دقیق رشته‌ی hostname را برمی‌گرداند؛ روی buffer خراب/کوتاه به‌جای exception مقدار `None` می‌دهد. روی ClientHelloِ واقعیِ `ClientHelloMaker` آفست = ۱۲۷ (دقیقاً منطبق با ثابت داخلی template).
  - **TCP segmentation:** `tcp_segment_at` (تقسیم در آفست دلخواه) + `tcp_segment_at_sni` (تقسیم داخل خود رشته‌ی SNI، با fallback روی نبود SNI) — کاملاً lossless.
  - **TLS record fragmentation:** `split_tls_records` یک record را به چند record کوچک‌تر با هدر معتبر می‌شکند (content-type/version حفظ می‌شود)؛ `reassemble_tls_records` معکوس آن برای اثبات lossless بودنِ *بدنه‌ی* handshake. توجه: stream نهایی به‌خاطر هدرهای اضافه‌ی record بزرگ‌تر می‌شود (byte-identical نیست) ولی به همان handshake بازچینی می‌شود.
  - **ورودی سطح‌بالا:** `fragment_client_hello(data, tcp=, tls=, tls_chunk=)` — لایه‌ها را به ترتیب اعمال می‌کند (اول TLS-record بعد TCP)؛ با هر دو خاموش، خودِ `data` را به‌صورت یک chunk برمی‌گرداند تا caller همیشه بتواند روی نتیجه iterate و send کند. این نقطه‌ی اتصال آینده برای engine/auto-prober است.
- **`core/config_store.py`** — کلیدهای `fragment_tcp`/`fragment_tls`/`fragment_tls_chunk` به `DEFAULT_CONFIG` اضافه شد تا تنظیمات persist شود و نقطه‌ی wiring صریح باشد (اعمال زنده در ProxyServer که pydivert لازم دارد، به‌طور طبیعی کنار Auto-Prober استپ ۹ سیم‌کشی می‌شود).
- **باگ یافته‌شده و حل‌شده (همین استپ):** نسخه‌ی record-layer در ClientHelloِ tool‏ مقدار `0x0301` است نه `0x0303` (TLS 1.3 برای سازگاری). دو تست که `0x0303` را hard-code کرده بودند fail شدند؛ به استخراج version واقعیِ همان record اصلاح شدند. ضمناً فرض اشتباه «byte-identical بودن stream بعد از TLS-fragmentation» در دو تست اصلاح شد (stream رشد می‌کند، فقط بازچینیِ بدنه lossless است).
- تست‌ها: `tests/test_fragment.py` (۲۲ تست، روی ClientHelloِ واقعی ۵۱۷ بایتی) — پارس هدر، یافتن SNI با طول‌های مختلف + ورودی غیر-TLS/کوتاه، segmentation و straddle شدن SNI، fragmentation رکورد و صحت هدر/اندازه‌ها، رد chunk_size نامعتبر، و ترکیب دو لایه. مجموعاً **۹۴ تست سبز**.

## ✅ استپ ۹ — جزئیات پیاده‌سازی (2026-05-29)
- **`core/prober.py`** — Auto-Prober، UI-agnostic و **شبکه تزریق‌پذیر** (probe_fn) تا کل منطق ranking/selection/health بدون socket روی sandbox تست شود:
  - `Candidate` (استراتژی inject + گزینه‌های fragmentation، با `key` یکتا مثل `wrong_seq+ftcp+ftls48`).
  - `ProbeResult` با `outcome` (OK/RST/TIMEOUT/ERROR) و `score()` آگاه به latency (۱.۰ در ۰ms، نزولی ملایم).
  - `HealthWindow` — sliding-window کران‌دار با `success_rate`/`mean_score`/`healthy` (آستانه ۰.۵).
  - `AutoProber` — `probe_all` (probe همه + ثبت health)، `select_best` (انتخاب بیشترین mean_score + قفل، tie-break تصادفی برای نبود امضای ثابت)، `run`، و برای پایش زنده: `record_live`/`needs_reprobe`/`fallback_order`. probeِ پرتابی هرگز run را نمی‌کشد (به ERROR تبدیل می‌شود).
  - `tcp_probe` — probe واقعی stdlib-only (فقط روی ویندوز اجرا؛ در sandbox با fake جایگزین می‌شود).
  - `build_candidates` — ساخت لیست کاندیدا از کلیدها + واریانت fragmentation.
- **`core/engine.py`** — متد `_choose_bypass_method(host, port)`: اگر `auto_prober` روشن باشد کاندیداها را از استراتژی‌های implemented (مرتب بر اساس prior = `score()`) می‌سازد، probe می‌کند و بهترین را قفل می‌کند؛ روی هر خطا/نبود host به‌صورت fail-soft به روش پیکربندی‌شده برمی‌گردد (Start هرگز روی prober بلاک نمی‌شود). `self._prober` در `__init__` مقداردهی شد.
- **`core/config_store.py`** — کلید `probe_timeout` (پیش‌فرض ۵.۰ ثانیه) به `DEFAULT_CONFIG`.
- **`ui/window.py`** — toggle «پراب خودکار» در StrategyPage حالا واقعی است: checkable، وضعیت اولیه از store، با `_on_autoprobe_toggled` مقدار `auto_prober` را persist می‌کند و سیگنال `auto_prober_changed` می‌دهد؛ MainWindow آن را به `engine.update_config` وصل می‌کند (toggle ↔ config ↔ engine، تست‌شده end-to-end به‌صورت headless).
- تست‌ها: `tests/test_prober.py` (۲۰ تست، probe جعلی دترمینیستیک) + ۲ تست integration در `tests/test_engine.py` (انتخاب برنده با probe جعلی، و fallback وقتی همه شکست می‌خورند). مجموعاً **۱۱۶ تست سبز**.
- **یادداشت بازیابی (2026-05-29):** پس از ری‌ست sandbox، کار استپ ۹ از بکاپ کاربر (`ZInsV2ss`) بازیابی شد. بکاپ شامل تغییرات `engine.py`/`config_store.py`/`ui/window.py`/`ARCHITECTURE.md`/`test_engine.py` بود؛ فایل‌های `core/prober.py` و `tests/test_prober.py` (که در بکاپ نبودند) از محتوای جلسه بازسازی شدند. صحت‌سنجی: ۱۱۶ تست سبز + smoke test موفق UI.

## ✅ استپ ۱۰ — جزئیات پیاده‌سازی (2026-05-29)
هدف: بقا در برابر سانسور **فعال** (نه فقط مسدودسازی منفعل). DPI پیشرفته دو کار می‌کند: ۱) RST جعلی تزریق می‌کند تا اتصال را قطع کند، ۲) به‌جای قطع، throttle می‌کند تا ابزار خراب به‌نظر برسد. این لایه هر دو را به سیگنال تبدیل می‌کند و با `fallback_order` پرابر، استراتژی/IP را می‌چرخاند.
- **`core/resilience.py`** — لایه‌ی تاب‌آوری، کاملاً pure-data و **UI/شبکه‌مستقل** (هیچ import از pydivert یا Qt) تا روی sandbox تست شود:
  - `RstClassifier` — تشخیص RST **جعلی** از واقعی با هیوریستیک موقعیت/زمان: اگر ServerHello یا داده‌ی اپ دیده شده باشد → LEGIT؛ اگر زودهنگام (≤ پنجره‌ی ۲۰۰ms) و بدون handshake → FORGED (همان reset مبتنی بر SNI)؛ TTL ناهمخوان نیز FORGED را تقویت می‌کند. RST جعلی نادیده گرفته می‌شود (سبک zapret).
  - `ThroughputMonitor` — پنجره‌ی لغزان نرخ بایت؛ `recent_bps` فقط روی **دنباله‌ی اخیر** (`min_samples`) میانگین می‌گیرد تا نمونه‌های قدیمیِ پرسرعت، افت تازه را پنهان نکنند؛ `is_throttled` وقتی نرخ اخیر < `throttle_ratio × baseline` بماند.
  - `ResilienceController` — سیاست واکنش: RST جعلی → `IGNORE_RST` تا سقف `rst_budget`، سپس `ROTATE_STRATEGY`؛ throttle → چرخش فوری استراتژی؛ پایان استراتژی‌ها → `ROTATE_IP`؛ پایان همه → `GIVE_UP`. زنجیره‌های استراتژی/IP از بیرون set می‌شوند تا با `fallback_order` پرابر هماهنگ شود.
- **`core/engine.py`** — متد `_build_resilience(primary_method, connect_ip)`: هنگام Start یک `ResilienceController` می‌سازد؛ زنجیره‌ی استراتژی = روش انتخابی + خروجی `fallback_order()` پرابر (یا بقیه‌ی استراتژی‌های implemented)، زنجیره‌ی IP = `CONNECT_IP` + `CONNECT_IP_ALTS`. کنترلر از طریق property `engine.resilience` در دسترس است و در صورت پشتیبانی، به `ProxyServer.resilience` تحویل داده می‌شود تا runtime ویندوز هنگام دیدن RST/افت throughput با آن مشورت کند. روی هر خطا fail-soft (Start بلاک نمی‌شود)؛ روی stop پاک می‌شود.
- **`core/config_store.py`** — کلیدهای `resilience` (پیش‌فرض True)، `rst_budget` (۳)، `throttle_ratio` (۰.۴) به `DEFAULT_CONFIG`.
- تست‌ها: `tests/test_resilience.py` (۲۰ تست: کلاسبندی RST جعلی/واقعی/TTL، تشخیص/بازیابی throttle، بودجه/چرخش/پایان کنترلر) + ۳ تست integration در `tests/test_engine.py` (ساخت و تحویل کنترلر، خاموش‌کردن resilience، گنجاندن IPهای اضافی). مجموعاً **۱۳۹ تست سبز**.
- **اعمال زنده:** خود drop کردن RST و چرخش استراتژی/IP در حین جلسه در runtime ویندوز (`fake_tcp.py`/`main.py` با pydivert) رخ می‌دهد؛ engine کنترلر و زنجیره‌ها را آماده/قفل می‌کند و آن را در اختیار proxy می‌گذارد (سیم‌کشی صریح، آزمون‌پذیر).

## ✅ استپ ۱۱ — جزئیات پیاده‌سازی (2026-05-29)
هدف: anti-dictation — وقتی سانسور یک ترفند را در سطح ملی مسدود کرد، با آپدیت یک فایل **داده** (نه کد) و **امضاشده** ترفند جدید پخش شود، بدون انتشار نسخه‌ی جدید اپ.
- **`core/ed25519.py`** — وریفایر Ed25519 (RFC 8032) **خالص پایتون، فقط verify**، بدون هیچ وابستگی (نه `cryptography` نه `PyNaCl`) تا exia تک‌فایلی PyInstaller سبک بماند و کد قابل‌ممیزی باشد. کلید خصوصی آفلاین نزد maintainer می‌ماند؛ اپ فقط داده‌ی عمومیِ منتشرشده را verify می‌کند. روی بردار تست RFC 8032 و round-trip با signer تست‌شده.
- **`core/strategies_remote.py`** — کانال آپدیت امضاشده، **داده‌ی خالص و شبکه‌تزریق‌پذیر** (fetcher بیرونی → روی sandbox بدون HTTP واقعی تست می‌شود):
  - `Recipe` — رسپیِ اعلانی = کلید استراتژیِ شناخته‌شده + پارامترهای fragmentation (هیچ کد اجرایی؛ mirror آلوده نمی‌تواند چیزی اجرا کند). `key` مثل `fake_ttl+ftcp+ftls48`.
  - `Manifest.parse` — اعتبارسنجی ساختاری سخت (version صحیح، recipes غیرخالی، strategy ناخالی، tls_chunk مثبت...).
  - `canonical_bytes` — سریال‌سازی canonical (sorted keys، بدون whitespace) به‌عنوان پیام امضا تا signer/verifier بایت‌به‌بایت توافق کنند فارغ از فرمت فایل.
  - `verify_manifest` / `trusted_public_key` (کلید عمومیِ embed‌شده) / `urllib_fetcher` (fetcher واقعیِ stdlib برای runtime).
  - `StrategiesUpdater` — چند mirror را به‌ترتیب امتحان می‌کند؛ اولین payloadِ **به‌درستی امضاشده** با `version` اکیداً بزرگ‌تر را می‌پذیرد؛ هر شکست fetch/verify/parse فقط log و skip می‌شود (fail-closed روی trust؛ mirror بد نه downgrade می‌کند نه خراب). `to_candidates`/`score_priors` رسپی‌ها را به `Candidate` پرابر نگاشت می‌کند و استراتژی‌های ناشناختهٔ remote را بی‌سروصدا فیلتر می‌کند.
- **`core/engine.py`** — `_load_remote_strategies()`: اگر `remote_strategies` روشن و mirror تنظیم باشد، manifest را fetch/verify می‌کند؛ `_choose_bypass_method` در صورت موفقیت، **مجموعه‌ی کاندیدا + priorها** را از manifest می‌گیرد (وگرنه رجیستری محلی). fail-soft کامل (Start هرگز بلاک نمی‌شود).
- **`core/config_store.py`** — کلیدهای `remote_strategies` (پیش‌فرض False) و `strategies_mirrors` (لیست URLها).
- **`tools/sign_strategies.py`** — ابزار آفلاینِ maintainer: `keygen` (تولید جفت‌کلید + چاپ کلید عمومی برای embed) و `sign` (امضای manifest + self-verify). signerِ RFC 8032 خودکفا روی همان field-mathِ وریفایر، بدون وابستگی.
- **`strategies.example.json`** — نمونه‌ی فرمت manifest.
- تست‌ها: `tests/test_strategies_remote.py` (۲۴ تست: وریفایر Ed25519 + بردار RFC، sign/verify round-trip، رد امضا/کلید/دادهٔ دستکاری‌شده، parse/validation، canonicalization، mirror-walk/version/trust، نگاشت candidate) + ۲ تست integration در `tests/test_engine.py` (تغذیه‌ی پرابر از manifest امضاشده، fallback روی امضای بد). مجموعاً **۱۶۵ تست سبز** + round-trip واقعی keygen→sign→load با ابزار.
- **اعمال داغ زنده** (re-fetch دوره‌ای حین اجرا) به‌صورت طبیعی در استپ UI/runtime آینده اضافه می‌شود؛ منطق load/verify/merge اینجا کامل و آزمون‌پذیر است.

## ✅ استپ ۱۲ — جزئیات پیاده‌سازی (2026-05-29)
هدف: نمایش زنده‌ی «مغز» ابزار (Auto-Prober استپ ۹ + تاب‌آوری استپ ۱۰) در UI، بدون کوپل‌شدن GUI به internalهای core.
- **`core/diagnostics.py`** — لایه‌ی diagnostics **UI-agnostic و داده‌ی خالص** (بدون Qt/شبکه):
  - `CandidateStat` — سلامت اندازه‌گیری‌شده‌ی هر کاندیدا (key/strategy/samples/success_rate/mean_score/selected/last_outcome).
  - `DiagnosticsSnapshot` — یک عکسِ immutable از وضعیت زنده: status، استراتژی فعال، spoof_port، لیست کاندیداها، و فیلدهای تاب‌آوری (forged_rst_count/rst_budget/throttled/recent_bps/baseline_bps/زنجیره‌های strategy و IP). properties کمکی `has_probe_data` و `throttle_ratio`.
  - `snapshot(engine)` — از `engine._prober` (health/selected) و `engine.resilience` (throughput/chains) عکس می‌گیرد؛ **کاملاً tolerant**: نبود prober/resilience یا engine بی‌کار فقط مقادیر پیش‌فرض می‌دهد، نه crash. کاندیداها «selected اول، سپس بر اساس mean_score» مرتب می‌شوند.
- **`core/engine.py`** — متد `diagnostics()` که `core.diagnostics.snapshot(self)` را برمی‌گرداند (هر زمان امن).
- **`ui/engine_bridge.py`** — متد passthrough `diagnostics()`.
- **`ui/window.py`** — صفحه‌ی جدید **`DiagnosticsPage`**: یک **renderer نازک** که `engine.diagnostics()` را روی `QTimer` (هر ۱ ثانیه) poll می‌کند و فقط هنگام نمایشِ صفحه فعال است (`start_polling`/`stop_polling` در `_on_page_changed`). نمایش: کارت خلاصه (استراتژی فعال + وضعیت + پورت)، کارت throughput (نوار درصدِ recent/baseline + برچسب throttle + شمارش RST جعلی/بودجه + زنجیره‌های fallback)، و جدول کاندیداها (امتیاز/موفقیت/نمونه/outcome با نشانگر ★ برای انتخاب‌شده). آیتم navigation ششم «تشخیص» اضافه شد (Log به انتها رفت).
- تست‌ها: `tests/test_diagnostics.py` (۷ تست هسته با AutoProber/ResilienceController واقعی) + `tests/test_diagnostics_page.py` (۵ تست render headless آف‌اسکرین، با skip مودبانه در نبود Qt). مجموعاً **۱۷۷ تست سبز** + smoke test موفق MainWindow (۶ صفحه، toggle شدن polling).

## ✅ استپ ۱۳ — جزئیات پیاده‌سازی (2026-05-29) — استپِ آخرِ roadmap
هدف: تبدیل پروژه به یک **exe تک‌فایلیِ ویندوزی** با PyInstaller که باینری‌ها (xray/vwarp/wintun + WinDivert) را embed می‌کند، آیکون دارد و در صورت نبودِ دسترسی Administrator خودش را **elevate** می‌کند.
- **`core/admin.py`** — هلپرِ self-elevation **خالص و قابل‌تست روی هر OS**؛ فقط فراخوانیِ واقعیِ `ShellExecuteW` ویندوز-اونلی است:
  - `is_windows()` / `is_frozen()` (تشخیص `sys.frozen`).
  - `is_admin(checker=None)` — پیش‌فرض `ctypes.windll.shell32.IsUserAnAdmin()`؛ غیرِویندوز True (چیزی برای elevate نیست). `checker` برای تست تزریق می‌شود.
  - `relaunch_params(argv, frozen)` — مونتاژِ خالصِ `(exe, params)`: بیلدِ frozen آرگ‌ها را از `argv[1:]` می‌گیرد، حالتِ dev کلِ `argv`. از `subprocess.list2cmdline` برای quoting صحیح استفاده می‌کند.
  - `ensure_admin(argv, *, is_admin_checker, runner)` — فقط روی ویندوز و وقتی admin نیست relaunch می‌کند؛ True یعنی «relaunch شد، پروسه باید خارج شود». `runner` اجازه می‌دهد تست‌ها فراخوانی را بدون دست‌زدن به Win32 کپچر کنند.
- **`app.py`** — **entrypointِ نازکِ سطح‌بالا** که PyInstaller freeze می‌کند: ابتدا `ensure_admin(argv)` (اگر relaunch شد، `return 0`)، سپس `ui.app_qt.main(theme=...)`. فلگِ سبکِ `--theme {light,dark}` پشتیبانی می‌شود و فلگ‌های ناشناخته نادیده گرفته می‌شوند تا relaunch خراب نشود.
- **`SNISpoofer.spec`** — اسپکِ PyInstaller: `--onefile` معادل، **بدون کنسول** (`console=False`)، مپِ کلِ `bin/` به `bin/` (هماهنگ با `core/binary_utils.get_bin_dir()` و `_MEIPASS/bin`)، جمع‌آوریِ WinDivert از pydivert با `collect_data_files` + `collect_dynamic_libs`، آیکون، hidden-importها و excludeهای حجمی (tkinter/numpy/PIL/QtWebEngine...).
- **`scripts/build_exe.py`** — هلپرِ buildِ یک‌مرحله‌ای با **preflight** (باید ویندوز باشد، PyInstaller نصب باشد، `bin/` کامل باشد)، ساختِ آیکون در صورت نبودن، پاک‌سازیِ `build/`+`dist/`، اجرای PyInstaller و گزارشِ `dist/SNISpoofer.exe`. روی Linux import-safe است (فقط روی ویندوز واقعاً build می‌کند).
- **`scripts/make_icon.py` + `assets/app.ico`** — تولیدِ آیکونِ چندسایزه (16…256) با Pillow؛ سپرِ تیره با گلیفِ «S» نئونیِ سایان-تیل (#27e0c8) و رینگِ بنفش (#9b7bff) هماهنگ با theme.
- **`requirements-build.txt`** — وابستگی‌های build جدا (`pyinstaller`, `pillow`) تا requirements اصلیِ runtime تمیز بماند.
- تست‌ها: `tests/test_admin.py` (۱۳ تست، با ۲ مورد skip روی غیرِویندوز)، `tests/test_app_entry.py` (۵ تست، با Qt تزریق‌شده‌ی فِیک)، `tests/test_build_exe.py` (۶ تست: preflight/spec-parse/ico-header). مجموعاً **۱۹۴ تست سبز + ۷ skip**.

## ✅ نظافتِ پس از roadmap (2026-05-29) — CI و آرشیو
بعد از کاملِ شدن استپ‌های ۱…۱۳، کارهای پایانی:
- **آرشیوِ tkinter قدیمی** — `gui.py` و `gui_old2.py` با `git mv` به `legacy/` منتقل شدند (تاریخچه حفظ شد) و `legacy/README.md` توضیحِ بازنشستگی را دارد. هیچ کدِ فعالی به آن‌ها وابسته نیست.
- **workflowِ جدید در `ci/release.yml` آماده شد (کاربر یک‌بار به `.github/workflows/` می‌بَرَد — محدودیتِ push رباتِ گیت‌هاب)** — build خودکارِ exe روی **ویندوزِ GitHub Actions**:
  - روی هر push به `main` / هر PR / اجرای دستی → exe ساخته و به‌عنوان **Artifact** آپلود می‌شود (دانلود از تب Actions، **بدون نیاز به tag**).
  - روی push یک tag مثل `v1.0.0` → علاوه بر artifact، یک **GitHub Release** با فایل zip ساخته می‌شود.
  - از `SNISpoofer.spec` + `app.py` جدید استفاده می‌کند (نه `gui.py` قدیمی)؛ باینری‌ها از repo می‌آیند و در صورت غیاب با `scripts/download_bins.py` دانلود می‌شوند (fail-soft).
- **تست‌های نگهبان** در `tests/test_build_exe.py` اضافه شد (workflow باید spec را build کند نه gui.py؛ artifact آپلود شود؛ gui.py در `legacy/` باشد). مجموعاً **۱۹۷ تست سبز + ۷ skip**.
- **`BUILD.md`** — راهنمای کاملِ فارسیِ ساختِ exe (هم از GitHub Actions بدون نصبِ چیزی، هم دستی روی ویندوز) + عیب‌یابی؛ از README لینک شد.

## 📌 یادداشت‌های فنی
- WinDivert نیاز به admin دارد — اکنون توسط `core/admin.ensure_admin` در `app.py` با `ShellExecuteW(..,"runas",..)` خودکار elevate می‌شود.
- روی sandbox فقط import/syntax تست می‌شود؛ نمایش گرافیکی و WinDivert روی ویندوز کاربر است.
- `ClientHelloMaker` (۵۱۷ بایت) مرجع همه‌ی استراتژی‌های مبتنی بر fake است — دست‌نخورده می‌ماند.
- tkinter (`gui.py`, `gui_old2.py`) آرشیو شد → `legacy/`.
- **ساختِ exe**: نیازی به ثبتِ هیچ secret دستی در GitHub نیست؛ `GITHUB_TOKEN` به‌صورت خودکار توسط Actions تزریق می‌شود. فقط push کن یا tag بزن.

## ✅ استپ ۱۵ — جزئیات پیاده‌سازی (2026-05-29)
هدف (بازخورد ۲): به‌جای تنظیم دستی host/port، کاربر لینک را paste می‌کند → همه‌ی فیلدها خودکار استخراج و در یک فرم قابل‌ویرایش پر می‌شوند → بازبینی/ادیت → افزودن.
- **`ui/profile_dialog.py`** — `ProfileDialog(QDialog)`: فرم اسکرول‌شونده با چهار بخش (پایه/اعتبارنامه/ترنسپورت/امنیت) و ~۲۰ فیلد؛ هر فیلد بر اساس نوع (line/int/combo) ساخته می‌شود. `_load` همه‌ی فیلدها را از `Profile` پر می‌کند، `collect()` ویجت‌ها را به یک `Profile` معتبر برمی‌گرداند (با حفظ `raw`/`extra`). دکمه‌ی افزودن قبل از بستن `validate()` می‌کند و خطاها را با رنگ قرمز نشان می‌دهد (بدون بستن دیالوگ روی ورودی نامعتبر).
- **`ui/window.py`** — `ProfilesPage._import_link` بازنویسی شد: paste → `parse_link` → باز کردن `ProfileDialog` پرشده → روی Accept ذخیره و انتخاب. `_edit_selected` جدید: ویرایش پروفایل انتخاب‌شده (دکمه‌ی «✎ ویرایش» + دابل‌کلیک روی لیست) و ذخیره؛ اگر پروفایلِ فعال ویرایش شود، به engine هم re-emit می‌شود. دکمه‌های ویرایش/حذف با آیکون.
- **`ui/theme.py`** — استایل QSS برای `QDialog#ProfileDialog` + `QScrollArea#DialogScroll` (پس‌زمینه/حاشیه/گوشه‌ی گرد هماهنگ با تم).
- تست‌ها: `tests/test_profile_dialog.py` (۴ تست headless آف‌اسکرین: پرشدن همه‌ی فیلدهای vless، round-trip ادیت‌ها، بلاک‌شدن روی UUID خالی سپس موفقیت، پرشدن اعتبارنامه‌ی trojan). برای اجرای واقعی، PySide6 + libEGL در sandbox نصب شد → **۲۰۸ تست سبز + ۲ skip** (skipهای باقی‌مانده فقط admin ویندوزی).

## ✅ استپ ۱۶ — جزئیات پیاده‌سازی (2026-05-29)
هدف (بازخورد ۳): قبلاً کانفیگ فعال فقط با رنگ مشخص می‌شد و هیچ جزئیاتی نداشت. حالا هر آیتم یک کارت غنی است.
- **`ui/widgets.py`** — `ProfileRow(QFrame)`: ردیف کارت‌مانند با آیکون پروتکل (✨ vless / ◈ vmess / ⚔ trojan / 🔒 ss)، نام نمایشی، خط جزئیات مونواسپیس `proto · host:port`، بدج‌های ترنسپورت/امنیت (WS/GRPC/TLS/REALITY...)، و **پیل سبز «● فعال»** فقط روی پروفایل انتخاب‌شده. دکمه‌ی inline «✎» با سیگنال `edit` برای ویرایش مستقیم از روی ردیف.
- **`ui/window.py`** — `ProfilesPage.refresh` بازنویسی شد: به‌جای متن ساده، برای هر پروفایل یک `ProfileRow` می‌سازد و با `setItemWidget` رندر می‌کند؛ ردیف فعال از `selected_index` تعیین می‌شود؛ سیگنال `edit` هر ردیف به `_edit_index(idx)` وصل است. حالت خالی هم یک پیام راهنما نشان می‌دهد. `_edit_selected`/`_edit_index` ریفکتور شدند تا منطق ویرایش مشترک باشد.
- **`ui/theme.py`** — QSS کامل ردیف: `ProfileRow` (پس‌زمینه/هاور/حاشیه‌ی accent روی active)، `RowGlyph`/`RowName`/`RowDetail`/`ActivePill` (سبز)/`RowBadge`/`RowEdit` (هاور accent). انتخاب لیست شفاف شد تا خود ردیف وضعیت را نشان دهد.
- تست‌ها: `tests/test_profile_row.py` (۴ تست: نمایش نام/جزئیات/بدج‌ها، پیل فعال فقط وقتی active، نبود بدج روی tcp+none، شلیک سیگنال edit). مجموعاً **۲۱۲ تست سبز + ۲ skip** + smoke موفق ProfilesPage (رندر ردیف غنی + property active).

## ✅ استپ ۱۷ — جزئیات پیاده‌سازی (2026-05-29)
هدف (بازخورد ۹): پیش از اتصال بدانیم کدوم سرور پینگ پایین‌تر و دانلود بهتری دارد، و کدوم استراتژی اصلاً می‌تواند وصل شود — با امکان «انتخاب یک استراتژی برای پینگ‌گرفتن». لایه‌ی هسته، UI-agnostic و شبکه‌تزریق‌پذیر تا کاملاً روی sandbox تست شود.
- **`core/ping.py`** — موتور پینگ/لتنسی، **بدون Qt/pydivert**، با بازاستفاده‌ی مستقیم از `core/prober.py` (Candidate/AutoProber/ProbeResult/build_candidates/tcp_probe):
  - **Primitiveهای تزریق‌پذیر:** `tcp_latency` (یک نمونه‌ی واقعی stdlib، روی خطا `None`)، `tcp_throughput` (تخمین سبک دانلود KB/s با یک burst کوتاه — شاخص نسبی برای مقایسه‌ی سرورها). در تست با fake جایگزین می‌شوند.
  - **`PingResult`** — تجمیع چندنمونه‌ای: `best_ms`/`avg_ms`/`jitter_ms`/`loss`/`reachable` و `sort_key` که سرورهای رسیدنی و کم‌تأخیر را بالا و بی‌پاسخ‌ها را ته لیست می‌برد (loss را هم کمی جریمه می‌کند تا سرور سریعِ بی‌ثبات #۱ نشود).
  - **`PingTester`** — `ping_target`/`ping_profile` (تک‌سرور)، `ping_all`/`ping_profiles` (چندسرور، مرتب‌شده‌ی «کم‌پینگ‌ترین اول»)، و `best(...)`. خواندن samples/timeout/measure_download از پیکربندی. هر نمونه‌ی خراب run را نمی‌کشد.
  - **تست استراتژی هنگام پینگ:** `probe_strategies(target, strategies=None, ...)` چند استراتژی را با AutoProber پراب می‌کند و `StrategyPingReport` می‌دهد (`best`/`any_connected`/`summary`)؛ `strategies` تک‌عضوی = «پینگ با یک استراتژی انتخابی». `default_strategy_keys()` کلیدهای implemented را برمی‌گرداند. probe به‌صورت **late-bound** حل می‌شود تا monkeypatch روی `core.prober.tcp_probe` (تست/رانتایم) محترم شمرده شود.
  - **fail-soft کامل:** آدرس/پورت نامعتبر، لیست استراتژی خالی، یا probe پرتابی هرگز exception نمی‌دهد — فقط گزارشِ «وصل نشد/نامعتبر».
- **`core/engine.py`** — متدهای جدید (blocking، روی worker صدا زده می‌شوند، همگی fail-soft): `_ping_tester()` (ساخت tester از config)، `ping_profiles`/`ping_profile`، و `probe_strategies_for(profile, strategy=None)` که استراتژی pin‌شده را از آرگومان یا `config["ping_strategy"]` می‌گیرد.
- **`ui/engine_bridge.py`** — passthroughهای `ping_profiles`/`ping_profile`/`probe_strategies_for` تا صفحه‌ی UI استپ ۱۸ بدون کوپل‌شدن به internalها صدا بزند.
- **`core/config_store.py`** — کلیدهای `ping_samples` (۳)، `ping_timeout` (۳.۰)، `ping_measure_download` (True)، `ping_strategy` ("" = تست همه) به `DEFAULT_CONFIG`.
- تست‌ها: `tests/test_ping.py` (۲۲ تست: تجمیع best/avg/jitter/loss، مرتب‌سازی کم‌پینگ‌ترین، سینک‌شدن بی‌پاسخ‌ها، best/none، دانلود وقتی reachable، نگاشت profile→target، انتخاب بهترین استراتژیِ متصل، پین تک‌استراتژی، رد ورودی نامعتبر) + ۴ تست integration در `tests/test_engine.py` (رتبه‌بندی پینگ از طریق engine، تک‌پروفایل، انتخاب برنده‌ی استراتژی، پین تک‌استراتژی). مجموعاً **۲۳۸ تست سبز + ۲ skip**.
- **اعمال زنده:** اندازه‌گیری واقعی روی ویندوز با همان primitiveهای stdlib انجام می‌شود؛ UI (استپ ۱۸) این لایه را روی worker thread صدا می‌زند تا GUI بلاک نشود.

## ✅ استپ ۱۸ — جزئیات پیاده‌سازی (2026-05-29)
هدف (بازخورد ۹، بخش UI): سطح‌دادن هسته‌ی پینگ استپ ۱۷ به کاربر — پیش از Start، با چند کلیک ببیند کدوم سرور بهتر است و کدوم استراتژی وصل می‌شود. همه روی worker thread تا GUI هرگز یخ نزند.
- **`ui/window.py` — `PingWorker(QThread)`** (جدید): یک کارگر پس‌زمینه که کار blockingِ engine را اجرا می‌کند و دو سیگنال می‌دهد: `line(str)` برای هر ردیف نتیجه و `done(str)` برای خلاصه‌ی نهایی. سه نوع: `ping_all` (پینگ همه‌ی پروفایل‌ها، مرتب‌شده‌ی کم‌پینگ‌ترین + اعلام بهترین سرور)، `ping_one` (تک‌سرور)، `strategy` (تست استراتژی‌ها روی یک سرور؛ `strategy` تک‌عضوی = پین یک استراتژی). فرمت‌کننده‌ی `fmt_ping` نمایش `best/avg/jitter/loss/dl≈KB/s` با نشانگر ✔/✖. کل run داخل try/except تا هیچ خطایی thread را نکشد.
- **`ui/window.py` — `ProfilesPage`**: پارامتر جدید `engine` گرفت. کارت جدید «سنجش پیش از اتصال» با: دکمه‌های «📡 پینگ همه» و «📡 پینگ این سرور»، یک `QComboBox` انتخاب استراتژی (همه + ۵ استراتژی) کنار دکمه‌ی «🧪 تست استراتژی‌ها»، یک برچسب وضعیت، و یک `QPlainTextEdit#PingOutput` فقط‌خواندنی برای نتایج. متدها: `_start_ping_job` (push کردن config تازه به engine، قفل دکمه‌ها حین اجرا، جلوگیری از اجرای هم‌زمان، نگه‌داشتن worker تا GC نشود)، `_ping_line`/`_ping_done`، و `_ping_all`/`_ping_one`/`_test_strategies` با گارد روی نبود پروفایل/انتخاب.
- **`ui/window.py` — `MainWindow`**: `ProfilesPage(self.store, engine=self.engine)` تا پنل پینگ به bridge وصل شود.
- **`ui/theme.py`** — QSS برای `QPlainTextEdit#PingOutput` (مونواسپیس، هم‌سبک با لاگ).
- تست‌ها: `tests/test_ping_ui.py` (۶ تست headless: وجود کنترل‌ها + ۶ آیتم کمبو، فرمت/رتبه‌بندی ping_all + اعلام بهترین، ping_one، علامت‌گذاری برنده در تست استراتژی، پین تک‌استراتژی با عبور درست آرگومان به engine، و گاردِ نبودِ پروفایل/انتخاب). worker مستقیماً `run()` می‌شود تا flakiness نخ نباشد. مجموعاً **۲۴۴ تست سبز + ۲ skip** + smoke موفق MainWindow (پنل پینگ wired به engine).

## ✅ استپ ۱۹ — جزئیات پیاده‌سازی (2026-05-29)
هدف (بازخورد ۱۰): امکان استفاده از پروکسی روی گوشی/دستگاه‌های دیگر در همان شبکه‌ی محلی. پیش از این inboundهای xray روی `127.0.0.1` هاردکد بودند (فقط همان کامپیوتر). اشتراک LAN **opt-in** است چون پروکسی را روی شبکه باز می‌کند.
- **`core/xray_config.py`** — `build_config(..., listen="127.0.0.1")` پارامتر جدید گرفت؛ هر دو inbound (socks-in/http-in) از همین `listen` استفاده می‌کنند (`0.0.0.0` = اشتراک LAN).
- **`core/xray_manager.py`** — `XrayManager(..., listen="127.0.0.1")`؛ به `build_config` پاس داده می‌شود. تابع جدید `lan_ip_address()` (ترفند UDP-connect استاندارد، بدون ارسال واقعی پکت) IP محلی اولیه را برمی‌گرداند تا کاربر بداند در گوشی چه آدرسی بزند؛ روی خطا fallback به `127.0.0.1`. لاگ Start حالا هنگام روشن‌بودن LAN یک خط راهنما با `SOCKS5 <lan-ip>:<port>` چاپ می‌کند.
- **`core/engine.py`** — `allow_lan` از config خوانده می‌شود؛ `listen="0.0.0.0" if allow_lan else "127.0.0.1"` به XrayManager پاس می‌شود.
- **`core/config_store.py`** — کلید `allow_lan` (پیش‌فرض False) به `DEFAULT_CONFIG`.
- **`ui/window.py` — `SettingsPage`**: چک‌باکس «اشتراک LAN — پروکسی روی شبکه‌ی محلی باز شود (برای گوشی)» + برچسب راهنمای پویا که هنگام روشن‌بودن، آدرس `SOCKS5 <lan-ip>:<port>` را نشان می‌دهد و یادآوری می‌کند هر دو دستگاه باید روی یک Wi-Fi باشند؛ خاموش = «فقط 127.0.0.1». در `load_from`/`collect` ذخیره/بازیابی می‌شود.
- تست‌ها: `tests/test_xray_config.py` (+۴: listen پیش‌فرض localhost، listen=0.0.0.0 روی هر دو inbound، propagation از manager، رشته‌بودن lan_ip_address) + `tests/test_engine.py` (+۲: allow_lan→0.0.0.0، local→127.0.0.1) + `tests/test_ping_ui.py` (+۳: roundtrip تاگل، خاموش به‌صورت پیش‌فرض، نمایش آدرس هنگام روشن). مجموعاً **۲۵۳ تست سبز + ۲ skip** + smoke موفق SettingsPage.
- **امنیت:** opt-in و خاموش به‌صورت پیش‌فرض؛ کاربر آگاهانه روشن می‌کند. (فایروال ویندوز ممکن است اولین بار اجازه بخواهد — رفتار طبیعی.)

## ✅ استپ ۲۰ — جزئیات پیاده‌سازی (2026-05-30)
هدف (بازخورد ۸/۵b/۷): تبدیل داشبورد به یک «کنترل‌مرکز» واحد که همه‌چیز زنده در آن دیده شود؛ رفع نبودِ مصرف زنده، و پاسخ به «تونل است یا پروکسی؟».
- **`ui/widgets.py` — `Sparkline(QWidget)`** (جدید): اسپارک‌لاینِ دوسری (دانلود/آپلود) با QPainter خالص (بدون وابستگی نموداری). پنجره‌ی غلتانِ ظرفیت‌دار (`push`/`clear`)، مقیاس‌بندی خودکار y نسبت به peakِ روی‌صفحه، پُرشدگی گرادیانی + خطِ نرم، رنگ‌پذیر از پالت (`set_colors`).
- **`ui/window.py` — هلِپرها**: `fmt_bytes` (B/KB/MB/GB/TB)، `fmt_rate` («…/s»)، `mode_kind` (تشخیص tunnel در حالت‌های warp/psiphon/gaming، وگرنه proxy).
- **`ui/window.py` — `DashboardPage` بازطراحی**: عنوان به «کنترل‌مرکز»؛ نشان tunnel/proxy کنار وضعیت (بازخورد ۷)؛ کارت «مصرف زنده» با نرخ ↓/↑ و Sparkline؛ ردیف آمار حالا شامل «مصرف کل (↓/↑)»؛ نوار «تاب‌آوری». اسلات‌های جدید: `on_traffic` (push به اسپارک + نرخ + مجموع)، `set_resilience`، و `set_mode` که نشان را با property `kind` و re-polish جابه‌جا می‌کند؛ `set_status("idle")` تصویر زنده را ریست می‌کند.
- **`ui/window.py` — `MainWindow`**: اتصال `engine.traffic → dashboard.on_traffic`؛ تایمر ۱.۵ث `_pump_resilience` که از `engine.diagnostics()` خلاصه‌ی زنده (RST budget + زنجیره‌ی استراتژی + throttle) را به نوار داشبورد می‌دهد و فقط در حالت active اجرا می‌شود.
- **`ui/theme.py`** — QSS برای `#ModeBadge` (+حالت‌های `kind=tunnel/proxy`)، `#RateDown`/`#RateUp`، و `#Sparkline`.
- **محیط:** PySide6 6.11.1 + کتابخانه‌های ران‌تایم Qt (`libegl1`) دوباره نصب شدند (محیط سندباکس بین نوبت‌ها ریست شده بود).
- تست‌ها: `tests/test_dashboard.py` (۱۰ تست: هلِپرهای فرمت بدون Qt + اسپارک‌لاین ظرفیت/peak/clear + اسلات‌های داشبورد traffic/badge/resilience/idle-reset). مجموعاً **۲۶۳ تست سبز + ۲ skip (فقط Windows-only)** + smokeهای موفق MainWindow و `_pump_resilience` روی engine بی‌کار.

## ✅🐞 استپ ۲۱ — رفع باگ بحرانی XHTTP (2026-05-30)
**علت ریشه‌ای:** ترنسپورت XHTTP (و خویشاوندانش) اصلاً پشتیبانی نمی‌شد. `core/xray_config._stream_settings` نگاشت ترنسپورت را به `tcp/ws/grpc/http/h2/quic/kcp` محدود کرده بود؛ هر چیز دیگری (از جمله `xhttp`) بی‌صدا به `tcp` تنزل می‌یافت و یک outbound نادرست تولید می‌شد → سرور Cloudflare Worker دست‌داد را نمی‌پذیرفت → کاربر مجبور بود از V2RayTun استفاده کند. ادعای «بی‌نیازی از v2rayN» برای XHTTP عملاً برقرار نبود.
- **`core/profile.py`** — `TRANSPORTS` حالا شامل `xhttp`, `splithttp`, `httpupgrade`؛ فیلد جدید `mode` (برای `auto/packet-up/stream-up/stream-one`).
- **`core/share_link.py`** — گرفتن `mode=` در پارسرهای vless و trojan (`_first(qs,"mode")`) و vmess (`g("mode")`).
- **`core/xray_config.py`** — افزودن `xhttp` به نگاشت مجاز + نرمال‌سازی `splithttp→xhttp` (نام قدیمی Xray). ساخت بلوک‌های ترنسپورت: `xhttpSettings={host, path, mode||auto}` و `httpupgradeSettings={path, host}`. (طبق اسکیمای رسمی Transport در xtls.github.io.)
- **`ui/profile_dialog.py`** — افزودن ردیف «حالت XHTTP» به بخش ترنسپورت؛ `xhttp/...` به‌صورت خودکار در کمبوی ترنسپورت ظاهر می‌شوند چون از `TRANSPORTS` پر می‌شود. (collect از `_source.to_dict()` شروع می‌کند، پس `mode` حتی بدون ویرایش حفظ می‌شود.)
- **اعتبارسنجی با کانفیگ واقعی کاربر:** لینک VLESS+XHTTP+Cloudflare او درست پارس و به `network=xhttp` + `tlsSettings`(SNI+fp=chrome) + `xhttpSettings`(host/path/mode=auto) تبدیل شد — دقیقاً همان چیزی که v2rayN/Hiddify تولید می‌کند.
- تست‌ها: `tests/test_share_link.py` (+۳: XHTTP/Cloudflare واقعی، mode در trojan، mode در vmess) + `tests/test_xray_config.py` (+۴: xhttpSettings درست بدون نشت tcp/ws، نرمال‌سازی splithttp، mode پیش‌فرض auto، httpupgrade) + `tests/test_profile_dialog.py` (+۱: prefill/round-trip mode). مجموعاً **۲۷۱ تست سبز + ۲ skip**.

## ✅ استپ ۲۲ — پروکسی سیستم ویندوز + سوییچ «تونل/پروکسی سیستم» (2026-05-30)
**چرا:** بازخورد ۷ — کاربر می‌خواهد یک حالت «پروکسی سیستم» داشته باشد که هنگام اتصال، پروکسی ویندوز را روی پورت HTTP محلی تنظیم کند (مثل v2rayN/Clash) تا همهٔ مرورگرها/برنامه‌ها خودکار رد شوند؛ و با قطع اتصال خودکار برگردد. در مقابلِ حالت «تونل» که فقط برنامه‌های دستی‌تنظیم‌شده رد می‌شوند و تنظیمات ویندوز دست‌نخورده می‌ماند.
- **`core/system_proxy.py` (جدید)** — هلپر پروکسی سیستم. کلیدهای WinINET زیر `HKCU\...\Internet Settings` نوشته می‌شوند (`ProxyEnable`/`ProxyServer`/`ProxyOverride`) و سپس با `InternetSetOption` رفرش می‌شوند تا بی‌نیاز از logout اعمال شود.
  - **منطق خالص و تست‌پذیر روی هر OS** (مدل‌گرفته از `core/admin.py`): `format_proxy_server(host,port)` (اعتبارسنجی پورت، host خالی→loopback)، `normalise_bypass()` (لیست bypass تمیزِ `;`-جداشده با fallback به `DEFAULT_BYPASS`)، `desired_state(enable,host,port,bypass)` (محاسبهٔ مقادیر رجیستری).
  - `DEFAULT_BYPASS = "localhost;127.*;10.*;172.16.*;192.168.*;<local>"` تا ترافیک محلی/اینترانت از پروکسی رد نشود.
  - کلاس `SystemProxy` با backendهای تزریق‌پذیر (`writer`/`refresher`/`reader`) — متدهای `enable/disable/is_enabled`. بخش‌های واقعی winreg/ctypes فقط روی ویندوز و با `# pragma: no cover` اجرا می‌شوند؛ تست‌ها هرگز رجیستری واقعی را لمس نمی‌کنند.
- **`core/engine.py`** — `_maybe_enable_system_proxy(use_core)` در `_do_start` (پس از بالاآمدن xray): فقط اگر `config["system_proxy"]` روشن، `use_core` (xray در زنجیره)، و ویندوز باشد، پروکسی OS را به `127.0.0.1:<http_port>` می‌چرخاند. در `stop()` ابتدا `disable()` صدا زده می‌شود (قبل از تخریب xray/proxy) تا مرورگر به پورت مرده اشاره نکند. فاکتوریِ تزریق‌پذیر `_system_proxy_factory` افزوده شد تا چرخهٔ enable/disable بدون لمس رجیستری تست شود.
- **`core/config_store.py`** — پیش‌فرض `"system_proxy": False`.
- **`ui/window.py`** — در SettingsPage: چک‌باکس `chk_system_proxy` + برچسب راهنمای `proxy_hint` + `_update_proxy_hint` که تفاوت «تونل» و «پروکسی سیستم» را توضیح می‌دهد؛ بارگذاری/جمع‌آوری در `load_from`/`collect`.
- **چرا فقط در حالت‌های دارای xray:** در «SNI Only» اسپوفر یک forwarder شفاف است و پروکسی HTTP واقعی‌ای وجود ندارد که OS را به آن اشاره دهیم؛ پس toggle در آن حالت نادیده گرفته شده و لاگ می‌شود.
- تست‌ها: `tests/test_system_proxy.py` (+۱۰: منطق خالص + چرخهٔ enable/disable با `_FakeBackend` + swallow خطای reader) + `tests/test_engine.py` (+۳: enable روی start/disable روی stop، نادیده‌گرفتن در SNI Only، خاموش بودن پیش‌فرض) + `tests/test_ping_ui.py` (+۳: round-trip toggle، خاموش پیش‌فرض، تغییر hint). مجموعاً **۲۸۷ تست سبز + ۲ skip**.

## ✅ استپ ۲۳ — لاگ حرفه‌ای (timestamp + سطح‌بندی رنگی + فیلتر + شمارنده) (2026-05-30)
**چرا:** بازخورد ۱ — لاگ قبلی فقط یک `QPlainTextEdit` تک‌رنگ بود؛ کاربر می‌خواست لاگ مثل ابزارهای حرفه‌ای: زمان هر رویداد، رنگ بر اساس شدت، امکان فیلتر و شمارش.
- **`core/logbuffer.py` (جدید)** — تمام منطق به‌صورت خالص و OS-agnostic (الگوی `core/admin.py`/`core/system_proxy.py`)، پس بدون Qt تست می‌شود:
  - `classify(message)` — استنتاج سطح (`info/ok/warn/err`) از متن پیام؛ engine همچنان فقط رشته می‌فرستد ولی با کلیدواژه‌های فارسی/انگلیسی (`خطا`/`ناموفق`/`failed`→err، `هشدار`/`throttl`/`نادیده`→warn، `✓`/`برقرار شد`/`روشن شد`→ok) رنگ می‌گیرد. شدیدترین تطبیق برنده است (err > warn > ok).
  - `LogEntry` (message/level/ts با `stamp` به‌صورت `HH:MM:SS` و `format()`).
  - `matches(entry, level, query)` — فیلتر سطح + جستجوی متنِ case-insensitive (خالص).
  - `LogBuffer` — بافر کرانمند چرخشی با شمارندهٔ هر سطح؛ `add` (با evict قدیمی‌ترین و کاهش شمارنده)، `clear`، `filtered`، `counts_summary`.
- **`ui/window.py` — بازنویسی `LogPage`** — از `QPlainTextEdit` به `QTextEdit` (rich text برای رنگ هر خط). نوار ابزار: کمبوی فیلتر سطح (`cmb_level` با گزینه‌های فارسی)، جستجوی متن (`search`)، نوار شمارندهٔ زندهٔ رنگی (`counters`). `append` خط جدید را طبقه‌بندی، شمارش، و در صورت عبور از فیلتر افزایشی رندر می‌کند؛ تغییر فیلتر/جستجو کل نما را از بافر بازسازی می‌کند (`_rerender`). رنگ‌ها داخل ویجت تعریف شده‌اند تا QSS فقط مخصوص تم بماند. HTML پیام escape می‌شود تا markup نشکند.
- **`ui/theme.py`** — `#Log` از `QPlainTextEdit` به `QTextEdit` تغییر کرد؛ استایل `#LogFilter`/`#LogSearch`/`#LogCounters` افزوده شد.
- تست‌ها: `tests/test_logbuffer.py` (+۱۵: classify، format، matches، add/count/evict، clear، filtered، summary) + `tests/test_log_ui.py` (+۵: seed، طبقه‌بندی/شمارش، فیلتر سطح، جستجو، clear). مجموعاً **۳۰۷ تست سبز + ۲ skip**.

## ✅ استپ ۲۴ — استراتژی کلیک‌پذیر + کارت‌های سه‌بعدی + رفع drag پنجره (2026-05-30)
**چرا:** بازخورد ۴ (صفحهٔ استراتژی فقط نمایشی بود و انتخاب نداشت)، بازخورد ۵ (حس تخت — نیاز به کارت‌های معلق/سایه عمیق/هاور)، بازخورد ۶ (drag پنجرهٔ frameless کند و لرزان بود).
- **استراتژی کلیک‌پذیر (`ui/window.py` → `StrategyPage`)** — هر کارت استراتژی حالا قابل‌کلیک است (cursor دست، `mousePressEvent` روی کل کارت). کلیک:
  - `bypass_method` را در store ذخیره می‌کند و سیگنال جدید `strategy_selected(key)` را emit می‌کند؛
  - چون انتخاب دستی و پراب خودکار متقابلاً انحصاری‌اند، کلیک‌کردن `auto_prober` را خاموش می‌کند؛
  - کارت انتخاب‌شده با `setProperty("selected", True)` + برچسب «✓ انتخاب‌شده» متمایز می‌شود (re-polish QSS برای اعمال فوری).
  - راهنمای `pick_hint` وضعیت را توضیح می‌دهد (در حالت پراب خودکار، انتخاب دستی نادیده گرفته می‌شود).
  - **`MainWindow._on_strategy_selected`** — flag را save و به engine زندهٔ push می‌کند + لاگ + Toast.
- **کارت‌های سه‌بعدی/معلق** — `ui/theme.py`: استایل `#StrategyCard` با `:hover` (حاشیهٔ accent + پس‌زمینهٔ روشن‌تر) و `[selected="true"]` (حاشیهٔ ضخیم accent). `ui/widgets.py → Card.set_shadow(blur,y,color)` افزوده شد تا سایهٔ drop در زمان اجرا قابل تنظیم باشد؛ StrategyPage با `enterEvent/leaveEvent` یک hover-lift اعمال می‌کند (عمیق‌تر شدن سایه + بالاآمدن کارت) — کاری که QSS به‌تنهایی نمی‌تواند روی `QGraphicsDropShadowEffect` انجام دهد.
- **رفع drag پنجره (`ui/widgets.py → TitleBar`)** — به‌جای حلقهٔ دستیِ `move()` که پشت نشانگر می‌لرزید، حالا `QWindow.startSystemMove()` بومیِ سیستم‌عامل استفاده می‌شود (drag روان، سازگار با compositor/DPI/snap-assist). در صورت نبودِ native move، fallback به روش دستی حفظ شده است (`_begin_native_move()` همیشه bool برمی‌گرداند).
- تست‌ها: `tests/test_strategy_ui.py` (+۶: انتخاب اولیه از store، کلیک→انتخاب/persist/emit، کلیک→خاموش‌شدن auto-prober، پنهان‌شدن انتخاب در حالت auto، fallback امن `_begin_native_move`، `Card.set_shadow`). smoke-test کامل MainWindow هم سبز. مجموعاً **۳۱۳ تست سبز + ۲ skip**.

## ✅ رفع بازخورد نسخهٔ بیلدشده (۴ باگ گزارش‌شده از اپ ویندوزی) (2026-05-30)
**چرا:** کاربر نسخهٔ واقعیِ بیلدشدهٔ ویندوز را تست کرد و ۴ مشکل گزارش داد (با اسکرین‌شات). همهٔ آن‌ها ریشه‌یابی و رفع شدند.

### باگ ۱ — کانفیگ کار نمی‌کرد (VLESS وصل نمی‌شد، هنوز باید V2RayTun استفاده می‌شد)
- **ریشه:** حالت پیش‌فرض روی **«SNI Only»** بود؛ در این حالت اسپوفر فقط یک forwarder خام است و **xray اصلاً اجرا نمی‌شود** — یعنی کانفیگ VLESS/VMess/Trojan هرگز استفاده نمی‌شد. تولید xray-config همیشه درست بود؛ مشکل صرفاً انتخاب حالت بود.
- **اعتبارسنجی:** xray-config تولیدشده برای کانفیگ دقیقِ کاربر (`vless://…@104.19.44.75:443?…type=ws&host=hammm2.pages.dev&path=…vps.webtun.xyz:2087#CF…`) **عیناً** با خروجی V2RayTun مطابقت دارد — `wsSettings.path` با مسیر تودرتو دست‌نخورده می‌ماند.
- **رفع (`ui/window.py`, `core/engine.py`, `core/config_store.py`):**
  - حالت جدید **«Tunnel»** افزوده و به‌عنوان پیش‌فرض تنظیم شد (`MODES` با Tunnel شروع می‌شود؛ `DEFAULT_CONFIG["connection_mode"]="Tunnel"`؛ `load_from` پیش‌فرض Tunnel).
  - `MODE_HINTS` افزوده شد تا تفاوت هر حالت زیر کمبوی حالت توضیح داده شود (به‌ویژه اینکه در «SNI Only» کانفیگ استفاده نمی‌شود).
  - **سوییچ خودکار:** هنگام انتخاب یک پروفایل، اگر حالت روی «SNI Only» بود به‌صورت خودکار به «Tunnel» تغییر می‌کند (`_on_profile_selected`) + Toast اطلاع‌رسانی.
  - **هشدار محافظ:** `EngineController.wants_core_but_no_profile` افزوده شد؛ اگر کاربر در حالتِ نیازمندِ هسته باشد ولی کانفیگی انتخاب نکرده، در `_do_start` لاگ هشدار می‌دهد.

### باگ ۲ — پنجره از نوار عنوان جابه‌جا نمی‌شد و دکمهٔ مینیمایز کار نمی‌کرد
- **ریشه:** `WA_TranslucentBackground` روی پنجرهٔ frameless فعال بود که هم drag و هم مینیمایز را در نسخهٔ بیلدشده خراب می‌کرد.
- **رفع (`ui/window.py`):** حذف `WA_TranslucentBackground` و حذف فراخوانی `apply_backdrop`؛ افزودن پرچم‌های `WindowMinimizeButtonHint | WindowSystemMenuHint` به‌کنار `FramelessWindowHint`. (drag بومی از استپ ۲۴ با `startSystemMove()` قبلاً درست شده بود.)

### باگ ۳ — به‌هم‌ریختگی UI (فیلدهای کوچک/له‌شده/بریده، اسکرول خراب، متن صفحهٔ استراتژی نصفه)
- **ریشه:** ظرفِ اسپین‌باکس فقط ۳۸px ارتفاع داشت و اسپین‌باکس ۴۷px را می‌برید؛ متن نامرئی می‌شد.
- **رفع (`ui/window.py`, `ui/theme.py`):** min-heightهای صریح (ظرف ۷۴px، اسپین‌باکس ۴۲px)، بسته‌بندی پورت‌ها در `ports_wrap` (۷۸px) + فاصله‌گذاری فرم، فاصله قبل از هر چک‌باکس، و QSS کاملِ ورودی‌ها (`min-height`, `padding`, hover/focus، فلش‌های کمبو/اسپین‌باکس). بصری تأیید شد که «۴۰۴۴۳»/«۱۰۸۰۸» واضح دیده می‌شوند.

### باگ ۴ — طراحی UI مدرن/سه‌بعدی/شناور تحویل داده نشده بود
- **رفع (`ui/theme.py`):** پس‌زمینهٔ `RootBackdrop` به `qlineargradient` مورب + حاشیه + گردیِ ۱۴px؛ کارت‌ها با گرادیان عمودی (`card_grad_top→bottom`) + حاشیهٔ هایلایت بالایی؛ افزودن ۶ فیلد گرادیان به `Palette` (تیره و روشن) و تبدیل سطوح rgba به solid. (به‌همراه سایه/هاور/معلق کارت‌ها از استپ ۲۴.)

- **ابزار:** کشف شد PySide6 در سندباکس نصب نبود (تست‌های UI بی‌صدا skip می‌شدند)؛ PySide6 6.11.1 + libEGL نصب شد تا رندر واقعی و اجرای درست تست‌ها ممکن شود. رندر offscreen (`grab().save()`) برای تأیید بصری همهٔ صفحات (داشبورد/استراتژی/تنظیمات/پروفایل/لاگ) استفاده شد.
- تست‌ها: `tests/test_mode_fix_ui.py` (+۸: معناشناسی حالت در engine بدون Qt، hint حالت در SettingsPage، پرچم‌های پنجره غیرشفاف/دارای مینیمایز، سوییچ خودکار به Tunnel، حفظ حالت غیر-SNI صریح). مجموعاً **۳۲۱ تست سبز + ۲ skip**.
