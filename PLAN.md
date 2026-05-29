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
- [ ] استپ ۹ — **غول آخر: Auto-Prober** — تست خودکار استراتژی‌ها، ranking، انتخاب/قفل خودکار
- [ ] استپ ۱۰ — تاب‌آوری: تشخیص RST جعلی، throttle، چرخش CONNECT_IP/استراتژی، fallback chain
- [ ] استپ ۱۱ — strategies.json از راه دور (mirror + امضا) برای آپدیت بدون انتشار اپ
- [ ] استپ ۱۲ — صفحه‌ی Strategy/Diagnostics در UI (نمایش استراتژی فعال، نمودار سلامت، probeها)
- [ ] استپ ۱۳ — Packaging: PyInstaller (یک exe)، آیکون، تست build، bundle نهایی

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

## 📌 یادداشت‌های فنی
- WinDivert نیاز به admin دارد (`_ensure_admin` موجود حفظ شود).
- روی sandbox فقط import/syntax تست می‌شود؛ نمایش گرافیکی و WinDivert روی ویندوز کاربر است.
- `ClientHelloMaker` (۵۱۷ بایت) مرجع همه‌ی استراتژی‌های مبتنی بر fake است — دست‌نخورده می‌ماند.
- tkinter (`gui.py`, `gui_old2.py`) بعد از تثبیت UI جدید آرشیو/حذف می‌شود.
