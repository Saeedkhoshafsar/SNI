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
- [ ] استپ ۳ — **پارسر share-link + subscription** (vless/vmess/trojan/ss) → مدل پروفایل
- [ ] استپ ۴ — **یکپارچه‌سازی هسته v2rayN-style** + زنجیر خودکار spoofing زیر اتصال (حذف 127.0.0.1:40443 دستی)
- [ ] استپ ۵ — اتصال UI به هسته (پروفایل‌ها/start/stop/callbackها) + مدیریت config
- [ ] استپ ۶ — StrategyEngine: استخراج interface تکنیک + ثبت تکنیک‌های موجود (wrong_seq)
- [ ] استپ ۷ — افزودن تکنیک‌ها: wrong_checksum (فعال‌سازی)، fake_ttl، multi-fake، split/disorder
- [ ] استپ ۸ — لایه‌ی fragmentation: TCP split + TLS record fragmentation (مستقل از موقعیت)
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

## 📌 یادداشت‌های فنی
- WinDivert نیاز به admin دارد (`_ensure_admin` موجود حفظ شود).
- روی sandbox فقط import/syntax تست می‌شود؛ نمایش گرافیکی و WinDivert روی ویندوز کاربر است.
- `ClientHelloMaker` (۵۱۷ بایت) مرجع همه‌ی استراتژی‌های مبتنی بر fake است — دست‌نخورده می‌ماند.
- tkinter (`gui.py`, `gui_old2.py`) بعد از تثبیت UI جدید آرشیو/حذف می‌شود.
