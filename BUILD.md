# راهنمای ساخت فایل exe — SNI Spoofer

این سند توضیح می‌دهد چطور خروجیِ نهایی (`SNISpoofer.exe`) را بسازید. دو راه دارید:

1. **ساخت خودکار از روی GitHub** (پیشنهادی — نیازی به نصب چیزی روی کامپیوترتان نیست).
2. **ساخت دستی روی ویندوزِ خودتان**.

> نکته‌ی مهم: ساختِ exe **فقط روی ویندوز** ممکن است، چون باینری‌ها (`xray.exe`, `vwarp.exe`,
> `wintun.dll`, درایور WinDivert) و خودِ exe ویندوزی هستند. روی لینوکس/مک نمی‌توان exe ویندوزی ساخت.

---

## راه ۱: ساخت خودکار از GitHub (GitHub Actions)

ورک‌فلوی آماده در فایلِ **`ci/release.yml`** قرار دارد و روی یک ماشینِ ویندوزِ گیت‌هاب
به‌صورت رایگان build می‌گیرد. **هیچ متغیر یا secretی لازم نیست** — همه‌چیز خودکار است.

> ⚠️ **یک‌بار کارِ دستی (فقط همین یک‌بار):** به‌دلیلِ محدودیتِ امنیتیِ گیت‌هاب، ربات نمی‌تواند
> فایل‌های داخلِ `.github/workflows/` را مستقیماً push کند. پس فایلِ آماده‌ی `ci/release.yml`
> را باید **یک‌بار خودتان** به مسیرِ `.github/workflows/release.yml` کپی/جابه‌جا کنید:
>
> **ساده‌ترین راه (از وب‌سایتِ گیت‌هاب):**
> 1. در ریپو فایل `ci/release.yml` را باز کنید → دکمه‌ی ✏️ (Edit) را بزنید.
> 2. مسیرِ بالای فایل را از `ci/release.yml` به `​.github/workflows/release.yml` تغییر دهید.
> 3. **Commit changes** را بزنید. تمام — از این به بعد خودکار build می‌گیرد.
>
> **یا با گیت روی کامپیوترِ خودتان:**
> ```bash
> git mv ci/release.yml .github/workflows/release.yml
> git commit -m "enable windows build workflow"
> git push
> ```
> (نسخه‌ی فعلیِ `.github/workflows/release.yml` در ریپو قدیمی است و روی `gui.py`
> build می‌گیرد؛ با این جابه‌جایی، نسخه‌ی درست جایگزینش می‌شود.)

### آیا باید متغیری در GitHub ثبت کنم؟
**خیر.** ورک‌فلو از `secrets.GITHUB_TOKEN` استفاده می‌کند که **گیت‌هاب خودش به‌صورت خودکار
می‌سازد** و در هر اجرا در دسترس است. شما نیازی به ساختن توکن، Personal Access Token یا
هیچ متغیر دیگری ندارید. باینری‌های اصلی (`xray.exe`/`vwarp.exe`/...) هم از قبل داخلِ ریپو
هستند، پس مرحله‌ی دانلود معمولاً اصلاً اجرا نمی‌شود.

### این ورک‌فلو کِی اجرا می‌شود؟
1. **هر push روی شاخه‌ی `main`** یا **هر Pull Request** → exe ساخته می‌شود و به‌عنوان
   **Artifact** آپلود می‌شود (از تب Actions قابل دانلود).
2. **push کردن یک tag مثل `v1.0.0`** → علاوه بر artifact، یک **GitHub Release** هم ساخته
   می‌شود و exe + فایل zip را به‌صورت عمومی منتشر می‌کند.
3. **اجرای دستی** → از تب **Actions** دکمه‌ی «Run workflow» را بزنید (`workflow_dispatch`).

### روش A — دانلودِ exe به‌عنوان Artifact (ساده‌ترین)
1. به ریپو در گیت‌هاب بروید → تب **Actions**.
2. آخرین اجرای موفقِ workflow با نام **«Build Windows EXE»** را باز کنید
   (علامت ✅ سبز کنارش باشد).
3. پایینِ صفحه، بخش **Artifacts** → روی **`SNISpoofer-windows-x64`** کلیک کنید تا دانلود شود.
4. فایلِ zip را باز کنید → داخلش `SNISpoofer.exe` است.

> اگر دیدید هیچ اجرایی در Actions نیست، یعنی هنوز چیزی push نشده. کافی است یک تغییر
> کوچک روی `main` push کنید، یا دستی از «Run workflow» اجرا کنید.

### روش B — ساختِ یک Release رسمی (با لینکِ عمومیِ دانلود)
اگر می‌خواهید نسخه‌ای با لینکِ دائمی و شماره‌نسخه منتشر کنید، یک **tag** بسازید و push کنید:

```bash
git tag v1.0.0
git push origin v1.0.0
```

بعد از چند دقیقه:
- به تب **Releases** ریپو بروید.
- نسخه‌ی «SNI Spoofer v1.0.0» با فایل‌های `SNISpoofer.exe` و
  `SNISpoofer-v1.0.0-windows-x64.zip` آماده‌ی دانلودِ عمومی است.

---

## راه ۲: ساخت دستی روی ویندوزِ خودتان

اگر می‌خواهید روی کامپیوترِ ویندوزیِ خودتان build بگیرید:

### پیش‌نیازها
- **ویندوز** (۱۰ یا ۱۱، ۶۴ بیتی).
- **Python 3.10+** نصب‌شده (هنگام نصب گزینه‌ی «Add to PATH» را بزنید).

### مراحل
```powershell
# ۱) کلون یا دانلودِ ریپو
git clone https://github.com/hamijafayi-debug/SNI-Spoofing.git
cd SNI-Spoofing

# ۲) نصبِ وابستگی‌ها (runtime + build)
pip install -r requirements.txt -r requirements-build.txt

# ۳) ساختِ exe (دو راه معادل)
python scripts/build_exe.py
#   یا مستقیم:
#   pyinstaller --clean --noconfirm SNISpoofer.spec
```

خروجی در `dist\SNISpoofer.exe` ساخته می‌شود.

> اسکریپتِ `build_exe.py` قبل از ساخت یک **preflight** انجام می‌دهد: بررسی می‌کند که روی
> ویندوز باشید، PyInstaller نصب باشد و باینری‌های `bin/` کامل باشند؛ اگر مشکلی باشد با پیامِ
> واضح متوقف می‌شود. اگر آیکون نباشد خودش می‌سازد.

---

## اجرای exe ساخته‌شده

- روی `SNISpoofer.exe` دابل‌کلیک کنید.
- چون مسیرِ inject به **WinDivert** نیاز دارد و WinDivert دسترسیِ **Administrator** می‌خواهد،
  برنامه در اولین اجرا **خودش پنجره‌ی UAC را باز می‌کند** و با دسترسی ادمین دوباره اجرا می‌شود
  (این کار توسط `core/admin.py` انجام می‌شود؛ نیازی نیست دستی «Run as administrator» بزنید،
  هرچند اگر بخواهید می‌توانید).

---

## نکات و عیب‌یابی

| مشکل | راه‌حل |
|------|--------|
| در Actions هیچ build سبزی نیست | یک تغییر روی `main` push کنید یا از «Run workflow» دستی اجرا کنید. |
| ساختِ دستی می‌گوید «build target is Windows» | روی لینوکس/مک هستید؛ exe ویندوزی فقط روی ویندوز ساخته می‌شود. از راه ۱ (GitHub) استفاده کنید. |
| آنتی‌ویروس exe را قرنطینه می‌کند | طبیعی است (PyInstaller + دستکاریِ پکت). یک استثنا اضافه کنید یا از سورس build بگیرید. |
| `SmartScreen` هشدار می‌دهد | چون exe امضای دیجیتال ندارد. روی «More info → Run anyway» بزنید. |
| باینری‌ها غایب‌اند | روی ویندوز `python scripts/download_bins.py` را اجرا کنید (نیاز به اینترنت). |

---

## خلاصه‌ی یک‌خطی

- **ساده‌ترین راه:** تب **Actions** → آخرین build سبز → دانلودِ artifact `SNISpoofer-windows-x64`.
  **هیچ متغیر/secretی لازم نیست.**
- **انتشار رسمی:** `git tag v1.0.0 && git push origin v1.0.0` → دانلود از تب **Releases**.
- **ساختِ لوکال (ویندوز):** `pip install -r requirements.txt -r requirements-build.txt` سپس `python scripts/build_exe.py`.
