"""Lightweight runtime internationalisation for the SNI Spoofer UI (#6).

Design goals
------------
* **Zero ceremony at call sites.** Source strings stay in Persian (the
  product's primary language); wrapping them in :func:`tr` translates them to
  the *active* language at render time. An unknown string falls back to itself,
  so partial coverage never crashes or shows blanks.
* **Two languages**: ``"fa"`` (Persian, default) and ``"en"`` (English). The
  Persian→English map lives in :data:`_EN`.
* **Live switching.** :func:`set_language` flips the active language and runs
  every registered observer so open windows can rebuild their visible text.

The map is keyed by the *exact* Persian source string. For Persian, :func:`tr`
returns the input unchanged (it is already Persian), so only the English map
needs maintaining.
"""
from __future__ import annotations

from typing import Callable

# active language: "fa" (default) or "en"
_lang = "fa"

# observers notified on language change (so live UI can rebuild)
_observers: list[Callable[[str], None]] = []


# Persian → English. Keys MUST match the Persian source strings passed to tr().
_EN: dict[str, str] = {
    # --- nav ---
    "داشبورد": "Dashboard",
    "پروفایل‌ها": "Profiles",
    "تنظیمات": "Settings",
    "استراتژی": "Strategy",
    "تشخیص": "Diagnostics",
    "لاگ": "Log",
    # --- dashboard ---
    "کنترل‌مرکز": "Control Center",
    "وضعیت زنده‌ی تونل، مصرف و کنترل سریع روشن/خاموش":
        "Live tunnel status, usage and quick on/off control",
    "شروع": "Start",
    "قطع اتصال": "Disconnect",
    "آماده — متوقف": "Ready — stopped",
    "متوقف": "Stopped",
    "در حال اتصال…": "Connecting…",
    "در حال اتصال": "Connecting",
    "متصل — تونل فعال": "Connected — tunnel active",
    "متصل": "Connected",
    "اتصال برقرار شد — spoofing فعال": "Connected — spoofing active",
    "اتصال قطع شد": "Disconnected",
    "خطا — تلاش دوباره": "Error — retry",
    "خطا در اتصال — لاگ را ببینید": "Connection error — see the log",
    "منتظر شروع تونل…": "Waiting for the tunnel to start…",
    "تلاش دوباره": "Retry",
    "اتصالات فعال": "Active connections",
    "مصرف کل (↓/↑)": "Total usage (↓/↑)",
    "حالت": "Mode",
    "استراتژی فعال": "Active strategy",
    "مصرف زنده": "Live usage",
    "پروکسی محلی": "Local proxy",
    "تونل کامل": "Full tunnel",
    "تاب‌آوری: —": "Resilience: —",
    "تاب‌آوری: {text}": "Resilience: {text}",
    "استراتژی فعال: —": "Active strategy: —",
    # --- profiles page ---
    "وارد کردن لینک اشتراک‌گذاری یا سابسکریپشن (vless/vmess/trojan/ss)":
        "Import a share link or subscription (vless/vmess/trojan/ss)",
    "افزودن لینک‌ها": "Add links",
    "از کلیپ‌بورد": "From clipboard",
    "افزودن سابسکریپشن": "Add subscription",
    "سرورهای ذخیره‌شده": "Saved servers",
    "\u270e  ویرایش": "\u270e  Edit",
    "\U0001f5d1  حذف انتخاب‌شده": "\U0001f5d1  Delete selected",
    "سنجش پیش از اتصال": "Pre-connection check",
    "ببین کدوم سرور پینگ پایین‌تر/دانلود بهتری دارد و کدوم استراتژی وصل می‌شود":
        "See which server has lower ping/better download and which strategy connects",
    "\U0001f4e1  پینگ همه": "\U0001f4e1  Ping all",
    "\U0001f4e1  پینگ این سرور": "\U0001f4e1  Ping this server",
    "استراتژی برای تست:": "Strategy to test:",
    "همه‌ی استراتژی‌ها": "All strategies",
    "\U0001f9ea  تست استراتژی‌ها": "\U0001f9ea  Test strategies",
    "نتیجه‌ی پینگ/تست استراتژی اینجا نمایش داده می‌شود …":
        "Ping / strategy-test results appear here …",
    "یک یا چند لینک را اینجا بچسبانید — هر لینک در یک خط\n"
    "vless://…\ntrojan://…\nیا یک لینک سابسکریپشن":
        "Paste one or more links here — one per line\n"
        "vless://…\ntrojan://…\nor a subscription link",
    "هنوز پروفایلی اضافه نشده — یک لینک بچسبانید":
        "No profiles yet — paste a link",
    # toasts / status (profiles)
    "ابتدا یک پروفایل را انتخاب کنید": "Select a profile first",
    "ابتدا یک پروفایل وارد و انتخاب کنید": "Import and select a profile first",
    "ابتدا یک سرور را انتخاب کنید": "Select a server first",
    "ابتدا متن/URL سابسکریپشن را وارد کنید":
        "Enter the subscription text/URL first",
    "افزودن لغو شد": "Add cancelled",
    "موتور در دسترس نیست": "Engine unavailable",
    "هیچ پروفایلی برای پینگ نیست": "No profile to ping",
    "هیچ لینک معتبری یافت نشد": "No valid link found",
    "هیچ پروفایل معتبری در سابسکریپشن یافت نشد":
        "No valid profile found in the subscription",
    "پروفایل به‌روزرسانی شد": "Profile updated",
    "پروفایل حذف شد": "Profile deleted",
    "در حال سنجش …": "Measuring …",
    "یک سنجش در حال اجراست …": "A measurement is already running …",
    "یک پینگ در حال اجراست …": "A ping is already running …",
    "نوع سنجش ناشناخته": "Unknown measurement type",
    "خطا: {exc}": "Error: {exc}",
    "سنجش با خطا متوقف شد": "Measurement stopped with an error",
    "هیچ نتیجه‌ای — پروفایلی نیست یا خطا رخ داد":
        "No results — no profile or an error occurred",
    "هیچ سروری پاسخ نداد": "No server responded",
    "بهترین سرور: {label} ({ms:.0f}ms)": "Best server: {label} ({ms:.0f}ms)",
    "نتیجه‌ای دریافت نشد": "No result received",
    "استراتژی‌ای تست نشد (آدرس/کاندیدا نامعتبر)":
        "No strategy tested (invalid address/candidate)",
    "هیچ استراتژی‌ای وصل نشد": "No strategy connected",
    "بهترین استراتژی: {s} ({ms:.0f}ms)": "Best strategy: {s} ({ms:.0f}ms)",
    "افزودن پروفایل جدید": "Add new profile",
    "ویرایش پروفایل": "Edit profile",
    "لینک نامعتبر: {exc}": "Invalid link: {exc}",
    "پروفایل افزوده شد: {name}": "Profile added: {name}",
    "{added} پروفایل افزوده شد ({bad} لینک نامعتبر رد شد)":
        "{added} profiles added ({bad} invalid links skipped)",
    "{added} پروفایل افزوده شد": "{added} profiles added",
    "{added} پروفایل از سابسکریپشن افزوده شد":
        "{added} profiles added from subscription",
    "واکشی سابسکریپشن ناموفق: {exc}": "Subscription fetch failed: {exc}",
    "سرور فعال شد: {name}": "Server activated: {name}",
    # --- profile dialog ---
    "مقادیر از روی لینک پر شده‌اند — در صورت نیاز ویرایش کنید":
        "Values are filled from the link — edit if needed",
    "اطلاعات پایه": "Basic info",
    "اعتبارنامه": "Credentials",
    "ترنسپورت": "Transport",
    "امنیت / TLS": "Security / TLS",
    "نام نمایشی": "Display name",
    "پروتکل": "Protocol",
    "آدرس سرور": "Server address",
    "پورت": "Port",
    "رمز عبور": "Password",
    "روش رمزنگاری (SS)": "Cipher (SS)",
    "حالت XHTTP (auto/packet-up/…)": "XHTTP mode (auto/packet-up/…)",
    "Host هدر": "Host header",
    "مسیر / serviceName": "Path / serviceName",
    "نوع هدر": "Header type",
    "امنیت": "Security",
    "اثرانگشت (uTLS)": "Fingerprint (uTLS)",
    "انصراف": "Cancel",
    "افزودن": "Add",
    "ذخیره": "Save",
    "localhost": "localhost",
    "محلی (127.0.0.1)": "Local (127.0.0.1)",
    "آدرس/پورت محلی پر شد": "Filled local address/port",
    # --- mode hints ---
    "اتصال کامل از طریق کانفیگ انتخاب‌شده (VLESS/VMess/Trojan) با هسته‌ی xray + اسپوف SNI. برای استفاده از کانفیگ‌ها این حالت را انتخاب کنید.":
        "Full connection through the selected config (VLESS/VMess/Trojan) with the xray "
        "core + SNI spoofing. Pick this mode to actually use your configs.",
    "اسپوف SNI بدون لایه‌ی بیرونی Warp/Psiphon. اگر کانفیگی انتخاب شده باشد، xray هم اجرا و زیر اسپوفر زنجیر می‌شود (کانفیگ VLESS کار می‌کند). فقط وقتی هیچ کانفیگی انتخاب نشده باشد، صرفاً فورواردر خام برای دور زدن DPI روی HTTPS عادی اجرا می‌شود.":
        "SNI spoofing without an outer Warp/Psiphon layer. If a config is selected, xray also "
        "runs chained under the spoofer (VLESS configs work). Only when no config is selected "
        "does it run a raw forwarder to bypass DPI on plain HTTPS.",
    "کانفیگ + لایه‌ی Cloudflare Warp (نیازمند باینری warp).":
        "Config + a Cloudflare Warp layer (requires the warp binary).",
    "کانفیگ + لایه‌ی Psiphon (نیازمند باینری psiphon).":
        "Config + a Psiphon layer (requires the psiphon binary).",
    "کانفیگ + دو لایه‌ی Warp تو در تو.":
        "Config + two nested Warp layers.",
    "بهینه‌سازی تأخیر پایین برای بازی (TCP no-delay/fast-open).":
        "Low-latency optimisation for gaming (TCP no-delay/fast-open).",
    # --- settings ---
    "حالت اتصال، SNI و پورت‌ها": "Connection mode, SNI and ports",
    "حالت اتصال": "Connection mode",
    "SNI جعلی": "Fake SNI",
    "IP اتصال": "Connect IP",
    "پورت گوش‌دادن": "Listen port",
    "پورت SOCKS": "SOCKS port",
    # --- strategy page ---
    "زرادخانه‌ی روش‌های دور زدن DPI + پراب خودکار (غول مرحله آخر)":
        "Arsenal of DPI-bypass methods + auto-prober (final boss)",
    "پراب خودکار": "Auto-prober",
    "فعال ✓": "Active ✓",
    "بهترین استراتژی را خودکار آزمایش، رتبه‌بندی و قفل می‌کند":
        "Automatically tests, ranks and locks the best strategy",
    "روی هر استراتژی کلیک کنید تا به‌صورت دستی انتخاب/قفل شود.":
        "Click any strategy to manually select/lock it.",
    "برای انتخاب دستی، ابتدا پراب خودکار را خاموش کنید.":
        "To select manually, turn off the auto-prober first.",
    "پراب خودکار روشن است؛ انتخاب دستی نادیده گرفته می‌شود. ":
        "Auto-prober is on; manual selection is ignored. ",
    # --- diagnostics ---
    "وضعیت زنده‌ی پراب خودکار و تاب‌آوری":
        "Live auto-prober and resilience status",
    "توان عبوری (throughput)": "Throughput",
    "سرعت لحظه‌ای عبور داده از تونل را نشان می‌دهد. نوار، سرعت فعلی را با "
    "«خط پایه‌ی» همین اتصال مقایسه می‌کند تا اگر سانسورچی سرعت را خفه کرد "
    "(throttle) معلوم شود. تا وقتی متصل نشده‌اید یا ترافیکی رد و بدل نشده، "
    "داده‌ای برای نمایش نیست.":
        "Shows the live data rate through the tunnel. The bar compares the current "
        "speed to this connection's own “baseline” to reveal if the censor is "
        "throttling. Until you're connected or some traffic has flowed, there is "
        "nothing to show.",
    "کاندیداها (probe)": "Candidates (probe)",
    "بی‌کار": "Idle",
    "بدون داده": "No data",
    "سرعت فعلی: —": "Current speed: —",
    "سرعت فعلی: در انتظار ترافیک…": "Current speed: waiting for traffic…",
    "سرعت فعلی: — (متصل نیست)": "Current speed: — (not connected)",
    "تاب‌آوری غیرفعال است": "Resilience is disabled",
    "بدون داده — پس از اتصال و عبور ترافیک پر می‌شود":
        "No data — fills after connecting and passing traffic",
    "در حال ساختن خط پایه… (برای سنجش throttle کمی ترافیک لازم است)":
        "Building baseline… (a little traffic is needed to detect throttling)",
    "هنوز probe انجام نشده — هنگام اتصال با «پراب خودکار» پر می‌شود.":
        "No probe yet — fills on connect when the auto-prober runs.",
    "استراتژی فعال: {s}": "Active strategy: {s}",
    " · پورت {p}": " · port {p}",
    "وضعیت: {st}{port}": "Status: {st}{port}",
    "سرعت فعلی: {v}": "Current speed: {v}",
    "  ⚠ احتمال throttle!": "  ⚠ possible throttle!",
    "{pct}% از خط پایه — {recent} از {base}{tag}":
        "{pct}% of baseline — {recent} of {base}{tag}",
    "RST جعلی: {n} / بودجه {b}": "Forged RST: {n} / budget {b}",
    "زنجیره‌ی استراتژی: {chain}\nزنجیره‌ی IP: {ips}":
        "Strategy chain: {chain}\nIP chain: {ips}",
    "زنجیره‌ی fallback: —": "Fallback chain: —",
    "امتیاز": "Score",
    "موفقیت": "Success",
    "نمونه": "Samples",
    "وضعیت": "Status",
    # --- log page ---
    "رویدادهای زنده‌ی موتور": "Live engine events",
    "جستجو در لاگ…": "Search the log…",
    "همه": "All",
    "پاک‌سازی": "Clear",
    "سطح": "Level",
    "SNI Spoofer UI بارگذاری شد": "SNI Spoofer UI loaded",
    # --- profile row ---
    "● فعال": "● ACTIVE",
    "فعال": "ACTIVE",
    "فعال‌سازی": "Activate",
    "فعال‌سازی این سرور": "Activate this server",
    "ویرایش این پروفایل": "Edit this profile",
    "پینگ این سرور": "Ping this server",
    "در حال پینگ…": "Pinging…",
    "بدون پاسخ": "No response",
    "✖ بدون پاسخ": "✖ No response",
    "سروری انتخاب نشده": "No server selected",
    "سروری انتخاب نشده — حالت SNI Only": "No server selected — SNI Only mode",
    "سرور": "Server",
    # --- strategy descriptions ---
    "تزریق ClientHello جعلی با seq خارج از پنجره":
        "Inject a fake ClientHello with an out-of-window seq",
    "چک‌سام نامعتبر تا سرور دور بریزد":
        "Invalid checksum so the server discards the fake",
    "TTL کوتاه تا فقط به DPI برسد":
        "Short TTL so the fake only reaches the DPI box",
    "چند بسته جعلی پشت‌سرهم": "Several fake packets back-to-back",
    "بی‌نظمی عمدی در ترتیب بسته‌ها": "Deliberate packet-order disorder",
    # --- settings hints ---
    "خاموش — پروکسی فقط روی همین کامپیوتر (127.0.0.1) در دسترس است":
        "Off — proxy is only reachable on this PC (127.0.0.1)",
    "اشتراک LAN — پروکسی روی شبکه‌ی محلی باز شود (برای گوشی)":
        "LAN sharing — open the proxy on the local network (for phones)",
    "پروکسی سیستم — همه‌ی برنامه‌های ویندوز خودکار از تونل رد شوند":
        "System proxy — route all Windows apps through the tunnel automatically",
    "<IP این کامپیوتر>": "<this PC's IP>",
    "روشن — در گوشی، پروکسی SOCKS5 را روی {ip}:{port} تنظیم کنید "
    "(هر دو دستگاه باید روی یک شبکه/Wi-Fi باشند)":
        "On — on the phone, set a SOCKS5 proxy to {ip}:{port} "
        "(both devices must be on the same network/Wi-Fi)",
    "حالت «پروکسی سیستم»: هنگام اتصال، پروکسی ویندوز روی پورت HTTP "
    "محلی تنظیم می‌شود و با قطع اتصال خودکار برمی‌گردد. فقط در "
    "حالت‌های دارای xray (نه SNI Only) و روی ویندوز کار می‌کند.":
        "“System proxy” mode: on connect, the Windows proxy is set to the local "
        "HTTP port and reverts automatically on disconnect. Works only in "
        "xray-capable modes (not SNI Only) and on Windows.",
    "حالت «تونل»: فقط برنامه‌هایی که دستی روی پروکسی محلی تنظیم "
    "شده‌اند رد می‌شوند؛ تنظیمات ویندوز دست‌نخورده می‌ماند.":
        "“Tunnel” mode: only apps manually pointed at the local proxy are "
        "routed; Windows settings stay untouched.",
    # --- titlebar / misc ---
    "اطلاع": "Info",
    "هشدار": "Warning",
    "خطا": "Error",
    "موفق": "Success",
    "v3.0 · Windows": "v3.0 · Windows",
    # --- live log / toast messages ---
    "پروفایل فعال: {name}": "Active profile: {name}",
    "پروفایلی انتخاب نشده — حالت SNI Only":
        "No profile selected — SNI Only mode",
    "غیرفعال": "Disabled",
    "RST {n}/{b} · زنجیره {chain}{throttle}":
        "RST {n}/{b} · chain {chain}{throttle}",
    "استراتژی فعال: {m}": "Active strategy: {m}",
    "سرور جدید فعال شد — اتصال بازنشانی شد":
        "New server activated — connection reset",
    "راه‌اندازی مجدد خودکار برای اعمال سرور جدید…":
        "Auto-restarting to apply the new server…",
    "انتخاب شد: {name}": "Selected: {name}",
    "حالت به «Tunnel» تغییر کرد تا کانفیگ انتخاب‌شده واقعاً استفاده شود":
        "Mode switched to “Tunnel” so the selected config is actually used",
    "حالت به «Tunnel» تغییر کرد (برای استفاده از کانفیگ)":
        "Mode switched to “Tunnel” (to use the config)",
    "فعال شد": "enabled",
    "غیرفعال شد": "disabled",
    "پراب خودکار فعال شد": "Auto-prober enabled",
    "پراب خودکار غیرفعال شد": "Auto-prober disabled",
    "انتخاب دستی: {name} ({key})": "Manual selection: {name} ({key})",
    "استراتژی انتخاب شد: {name}": "Strategy selected: {name}",
}

_FA: dict[str, str] = {}        # identity for Persian; kept for symmetry


def available_languages() -> list[str]:
    return ["fa", "en"]


def language() -> str:
    return _lang


def set_language(lang: str) -> None:
    """Switch the active language and notify observers."""
    global _lang
    if lang not in ("fa", "en"):
        return
    if lang == _lang:
        return
    _lang = lang
    for cb in list(_observers):
        try:
            cb(lang)
        except Exception:
            pass


def toggle_language() -> str:
    set_language("en" if _lang == "fa" else "fa")
    return _lang


def on_language_changed(cb: Callable[[str], None]) -> None:
    """Register an observer run whenever the language changes."""
    if cb not in _observers:
        _observers.append(cb)


def tr(text: str) -> str:
    """Translate *text* (a Persian source string) to the active language.

    Persian returns the input unchanged; English looks it up in :data:`_EN`
    and falls back to the original if unknown.
    """
    if _lang == "en":
        return _EN.get(text, text)
    return text
