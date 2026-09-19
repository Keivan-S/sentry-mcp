# sentry-mcp

[English](README.md)

اتصال مستقیم Claude Desktop و Claude Code به **Sentry خودمیزبان (self-hosted)**.

سرور MCP رسمی Sentry جستجوی issue و event را به یک مدل زبانی دیگر (OpenAI یا Anthropic) می‌سپارد و برای این کار کلید API جداگانه لازم دارد. sentry-mcp روی همین کامپیوتر اجرا می‌شود و مستقیم با API خود Sentry (`/api/0/`) حرف می‌زند. نه سرویس میانی در کار است و نه مدل دوم؛ query جستجو را خود Claude با سینتکس Sentry می‌نویسد. اگر این سیستم Sentry را در مرورگر باز می‌کند، این سرور هم به آن می‌رسد.

## چه کارهایی از Claude برمی‌آید

| ابزار | کار | تغییر می‌دهد؟ |
|---|---|---|
| `sentry_info` | بررسی اتصال: آدرس، صاحب توکن، دسترسی‌ها (scopes)، سازمان‌ها | نه |
| `list_projects` / `list_teams` / `find_members` | پروژه‌ها، تیم‌ها و اعضای سازمان | نه |
| `search_issues` | جستجوی issueها با سینتکس Sentry (`is:unresolved level:error release:1.4.2`)، بازه‌ی زمانی، پروژه، محیط و ترتیب | نه |
| `get_issue` | جزئیات کامل issue: وضعیت، ریلیزها، پراکندگی تگ‌ها، کامنت‌ها و فعالیت‌ها، به‌علاوه‌ی یک event (آخرین، اولین، پیشنهادی یا با شناسه) | نه |
| `list_issue_events` | تک‌تک رخدادهای یک issue | نه |
| `get_event` | یک event با شناسه‌اش؛ اگر پروژه مشخص نباشد، خودش پیدا می‌کند | نه |
| `get_issue_tag_values` | پراکندگی یک issue روی یک تگ: کدام URLها، ریلیزها، کاربرها یا سرورها | نه |
| `search_events` | کوئری Discover برای شمارش و گروه‌بندی (`["url", "count()"]`، `p95(transaction.duration)`) | نه |
| `list_releases` | ریلیزها با تعداد issueهای جدید و آخرین deploy | نه |
| `update_issues` | resolve، resolve در ریلیز بعدی، بازگشایی، archive، واگذاری (`me`، ایمیل، `#team`)، اولویت، bookmark، دیده‌شده، اشتراک | issue |
| `add_issue_comment` | کامنت روی issue | issue |

با `SENTRY_READ_ONLY=true` این دو ابزار آخر حذف می‌شوند.

eventها طوری برمی‌گردند که خواندنشان راحت باشد:
- نقطه‌ی خطا اول می‌آید و frameهای کد خود پروژه همراه خطوط سورس (و متغیرها، اگر SDK بفرستد) نشان داده می‌شوند.
- رشته‌های طولانی frameهای کتابخانه‌ای خلاصه می‌شوند.
- exceptionهای زنجیره‌ای از آخرین exception به علت‌هایش مرتب می‌شوند.
- request بدون cookie و هدرهای احراز هویت برمی‌گردد، همراه آخرین breadcrumbها (SQL، لاگ، درخواست‌های HTTP)، کاربر، تگ‌ها و contextها.

به issue می‌شود با شناسه‌ی عددی، شناسه‌ی کوتاه (`API-1A`) یا آدرسش اشاره کرد. زمان‌ها به UTC هستند.

## توکن

در Sentry یک **User Auth Token** بسازید: *User settings → Personal Tokens* (`<sentry>/settings/account/api/auth-tokens/`)، با این دسترسی‌ها:

- خواندن: `org:read`، `project:read`، `team:read`، `member:read`، `event:read`
- نوشتن (برای `update_issues` و `add_issue_comment`): `event:write`

توکن سازمانی (`sntrys_…`) کافی نیست؛ فقط برای آپلود ریلیز و source map است.

## نصب از Release (پیشنهادی)

فایل مناسب سیستم‌تان را از [Releases](../../releases) دانلود کنید:

| سیستم | فایل |
|---|---|
| ویندوز | `sentry-mcp-<version>-windows-x64.mcpb` |
| مک با تراشه‌ی Apple (M1 به بعد) | `sentry-mcp-<version>-macos-arm64.mcpb` |
| مک اینتل | `sentry-mcp-<version>-macos-x64.mcpb` |

1. روی فایل `.mcpb` دوبار کلیک کنید (یا از Claude Desktop ← Settings ← Extensions نصبش کنید).
2. آدرس Sentry (مثلاً `https://sentry.company.com`) و توکن را وارد کنید.
3. Claude Desktop توکن را در حافظه‌ی امن سیستم‌عامل نگه می‌دارد، نه در فایل تنظیمات.

Python لازم نیست؛ فایل اجرایی مستقل است. فایل‌های اجرایی جداگانه هم در Release هستند. این فایل‌ها امضای دیجیتال ندارند و ممکن است ویندوز هنگام دانلود هشدار SmartScreen نشان دهد.

## نصب از سورس

```powershell
cd D:\Projects\Packages\sentry-mcp
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

توکن را یک بار در **Windows Credential Manager** (در مک: Keychain) ذخیره کنید. توکن را بدون نمایش روی صفحه می‌گیرد، بعد با Sentry چکش می‌کند و صاحب توکن و دسترسی‌های کم‌وکسر را نشان می‌دهد:

```powershell
.\.venv\Scripts\sentry-mcp.exe set-token --url https://sentry.company.com
```

تست اتصال. صاحب توکن، سازمان‌ها و آخرین issueهای باز را چاپ می‌کند، یا دلیل روشن خطا را می‌گوید:

```powershell
$env:SENTRY_URL = "https://sentry.company.com"
.\.venv\Scripts\sentry-mcp.exe check
```

Claude Code:

```powershell
claude mcp add sentry -s user -e SENTRY_URL=https://sentry.company.com -- D:\Projects\Packages\sentry-mcp\.venv\Scripts\sentry-mcp.exe
```

Claude Desktop، در `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sentry": {
      "command": "D:\\Projects\\Packages\\sentry-mcp\\.venv\\Scripts\\sentry-mcp.exe",
      "env": { "SENTRY_URL": "https://sentry.company.com" }
    }
  }
}
```

بعد Claude Desktop را کامل ببندید (از آیکون کنار ساعت Quit کنید؛ بستن پنجره کافی نیست) و دوباره باز کنید.

## تنظیمات

نام متغیرها با sentry-cli یکی است و یک دسته متغیر برای هر دو کار می‌کند.

| متغیر | پیش‌فرض | معنی |
|---|---|---|
| `SENTRY_URL` | (الزامی) | آدرسی که Sentry را با آن باز می‌کنید، مثلاً `https://sentry.company.com` |
| `SENTRY_AUTH_TOKEN` | Credential Manager | راه جایگزین: توکن به‌صورت متن ساده |
| `SENTRY_ORG` | تنها سازمان | slug سازمان پیش‌فرض، وقتی توکن چند سازمان می‌بیند |
| `SENTRY_READ_ONLY` | `false` | با `true`، ابزارهای `update_issues` و `add_issue_comment` حذف می‌شوند |
| `SENTRY_USE_SYSTEM_CERTS` | `true` | به گواهی‌های مورد اعتماد سیستم‌عامل (CA داخلی) اعتماد کند |
| `SENTRY_CA_BUNDLE` | ندارد | فایل PEM گواهی CA داخلی، اگر سیستم‌عامل به آن اعتماد ندارد |
| `SENTRY_VERIFY_SSL` | `true` | با `false` بررسی TLS خاموش می‌شود (فقط آخرین راه) |
| `SENTRY_TIMEOUT` | `30` | ثانیه برای هر درخواست |
| `SENTRY_MCP_LOG_LEVEL` | `WARNING` | سطح لاگ روی stderr |

ریدایرکت‌ها دنبال نمی‌شوند تا توکن هیچ‌وقت به آدرس دیگری فرستاده نشود. اگر Sentry ریدایرکت کند (مثلاً از `http://` به `https://`)، پیام خطا آدرس درست را برای `SENTRY_URL` می‌گوید.

## توسعه

```bash
pip install -e ".[dev,build]"
pytest                        # روی یک API جعلی Sentry اجرا می‌شود (tests/fake_sentry.py)
python packaging/build.py     # فایل اجرایی مستقل و .mcpb برای همین سیستم، در dist/
```

با push یک تگ `v*`، GitHub Actions نسخه‌های ویندوز x64، مک arm64 و مک x64 را می‌سازد، تست‌های MCP را روی هر فایل اجرایی ساخته‌شده اجرا می‌کند و همه را به یک Release در GitHub ضمیمه می‌کند.
