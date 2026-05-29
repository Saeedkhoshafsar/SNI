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
- [ ] استپ ۱۵ — دیالوگ ادیت پروفایل: paste لینک → پارس → فرم پرشده‌ی قابل‌ویرایش → افزودن/ذخیره (بازخورد ۲)
- [ ] استپ ۱۶ — لیست پروفایل غنی: برچسب «● فعال»، جزئیات سرور (پروتکل/آدرس/transport/امنیت)، آیکون پروتکل، دکمه‌ی ویرایش (بازخورد ۳)
- [ ] استپ ۱۷ — داشبورد واحد «کنترل‌مرکز»: نمودار زنده‌ی آپلود/دانلود، مصرف کل، استراتژی فعالِ زنده، وضعیت resilience، حالت tunnel/proxy (بازخورد ۸ و ۵b و ۷)
- [ ] استپ ۱۸ — پروکسی سیستم ویندوز (set/unset رجیستری) + سوییچ در UI «تونل/پروکسی سیستم» (بازخورد ۷)
- [ ] استپ ۱۹ — لاگ حرفه‌ای: timestamp، سطح‌بندی info/ok/warn/err با رنگ، فیلتر، شمارنده (بازخورد ۱)
- [ ] استپ ۲۰ — صفحه‌ی استراتژی کلیک‌پذیر + بازطراحی تم سه‌بعدی/مدرن (کارت‌های معلق/شیشه/سایه عمیق/هاور) + رفع drag پنجره (بازخورد ۴/۵/۶)

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
