# Formats -- Locale-Aware Formatting

Locale-aware formatting for dates, times, numbers, currencies, and timezones. Wires all 15 i18n-related settings from `hyperdjango.conf` into actual formatting behavior. Django-compatible format characters for dates/times, locale-aware number formatting, input parsing, and timezone operations.

```python
from hyperdjango.formats import (
    format_date,
    format_number,
    format_currency,
    now,
    localtime,
)
from datetime import date

format_date(date.today())  # "March 29, 2026"
format_date(date.today(), "m/d/Y")  # "03/29/2026"
format_number(1234567.89)  # "1,234,567.89"
format_currency(1234.5)  # "$1,234.50"

dt = now()  # datetime with UTC tzinfo
local = localtime(dt, "US/Eastern")  # converted to Eastern
```

---

## Date/Time Formatting

### `format_date(value, format_string=None)`

Format a `date` or `datetime` using Django-style format characters. If `format_string` is `None`, the `DATE_FORMAT` setting is used.

```python
from hyperdjango.formats import format_date
from datetime import date, datetime

format_date(date(2026, 3, 29))  # uses DATE_FORMAT setting
format_date(date(2026, 3, 29), "m/d/Y")  # "03/29/2026"
format_date(date(2026, 3, 29), "F jS, Y")  # "March 29th, 2026"
format_date(date(2026, 3, 29), "D, M j")  # "Sun, Mar 29"
```

### `format_datetime(value, format_string=None)`

Format a `datetime` using `DATETIME_FORMAT` setting or a custom format string.

```python
from hyperdjango.formats import format_datetime
from datetime import datetime, timezone

dt = datetime(2026, 3, 29, 14, 30, 0, tzinfo=timezone.utc)
format_datetime(dt)  # uses DATETIME_FORMAT setting
format_datetime(dt, "Y-m-d H:i:s")  # "2026-03-29 14:30:00"
format_datetime(dt, "l, F jS Y \\a\\t g:i A")  # "Sunday, March 29th 2026 at 2:30 PM"
```

### `format_time(value, format_string=None)`

Format a `time` or `datetime` using `TIME_FORMAT` setting or a custom format string.

```python
from hyperdjango.formats import format_time
from datetime import time

format_time(time(14, 30))  # uses TIME_FORMAT setting
format_time(time(14, 30), "g:i A")  # "2:30 PM"
format_time(time(14, 30), "H:i")  # "14:30"
format_time(time(0, 0), "P")  # "midnight"
format_time(time(12, 0), "P")  # "noon"
format_time(time(14, 30), "P")  # "2:30 p.m."
```

### `format_short_date(value)`

Format using the `SHORT_DATE_FORMAT` setting.

```python
from hyperdjango.formats import format_short_date
from datetime import date

format_short_date(date(2026, 3, 29))  # "03/29/2026" (default setting)
```

### `format_short_datetime(value)`

Format using the `SHORT_DATETIME_FORMAT` setting.

```python
from hyperdjango.formats import format_short_datetime
from datetime import datetime, timezone

dt = datetime(2026, 3, 29, 14, 30, tzinfo=timezone.utc)
format_short_datetime(dt)  # "03/29/2026 2:30 p.m."
```

---

## Format Character Reference

All Django-compatible format characters are supported. Backslash-escape any character to output it literally (`\a` outputs `a`).

### Day

| Char | Description                              | Example                           |
| ---- | ---------------------------------------- | --------------------------------- |
| `d`  | Day of month, 2 digits with leading zero | `01` to `31`                      |
| `j`  | Day of month without leading zero        | `1` to `31`                       |
| `D`  | Day of week, 3-letter abbreviation       | `Mon`, `Tue`, ... `Sun`           |
| `l`  | Day of week, full name                   | `Monday`, `Tuesday`, ... `Sunday` |
| `S`  | English ordinal suffix for day           | `st`, `nd`, `rd`, `th`            |
| `w`  | Day of week (0=Sunday)                   | `0` to `6`                        |
| `z`  | Day of year                              | `1` to `366`                      |

### Month

| Char | Description                       | Example                               |
| ---- | --------------------------------- | ------------------------------------- |
| `m`  | Month, 2 digits with leading zero | `01` to `12`                          |
| `n`  | Month without leading zero        | `1` to `12`                           |
| `M`  | Month, 3-letter abbreviation      | `Jan`, `Feb`, ... `Dec`               |
| `N`  | Month, AP style abbreviation      | `Jan.`, `Feb.`, `March`, `April`, ... |
| `F`  | Month, full name                  | `January`, `February`, ... `December` |
| `t`  | Number of days in the month       | `28` to `31`                          |

### Year

| Char | Description    | Example      |
| ---- | -------------- | ------------ |
| `y`  | Year, 2 digits | `00` to `99` |
| `Y`  | Year, 4 digits | `2026`       |
| `L`  | Leap year flag | `0` or `1`   |

### Hour

| Char | Description                           | Example      |
| ---- | ------------------------------------- | ------------ |
| `G`  | Hour, 24-hour format, no leading zero | `0` to `23`  |
| `H`  | Hour, 24-hour format, leading zero    | `00` to `23` |
| `g`  | Hour, 12-hour format, no leading zero | `1` to `12`  |
| `h`  | Hour, 12-hour format, leading zero    | `01` to `12` |

### Minute / Second / Microsecond

| Char | Description            | Example              |
| ---- | ---------------------- | -------------------- |
| `i`  | Minutes, leading zero  | `00` to `59`         |
| `s`  | Seconds, leading zero  | `00` to `59`         |
| `u`  | Microseconds, 6 digits | `000000` to `999999` |

### AM/PM

| Char | Description                               | Example                         |
| ---- | ----------------------------------------- | ------------------------------- |
| `A`  | Uppercase AM/PM                           | `AM`, `PM`                      |
| `a`  | AP style am/pm                            | `a.m.`, `p.m.`                  |
| `P`  | AP time (special cases for midnight/noon) | `midnight`, `noon`, `2:30 p.m.` |

### Timezone

| Char | Description           | Example             |
| ---- | --------------------- | ------------------- |
| `e`  | Timezone name         | `UTC`, `US/Eastern` |
| `O`  | UTC offset (+HHMM)    | `+0000`, `-0500`    |
| `T`  | Timezone abbreviation | `UTC`, `EST`        |
| `Z`  | UTC offset in seconds | `0`, `-18000`       |

### Full Formats

| Char | Description                          | Example                           |
| ---- | ------------------------------------ | --------------------------------- |
| `U`  | Unix timestamp (seconds since epoch) | `1774992600`                      |
| `c`  | ISO 8601 format                      | `2026-03-29T14:30:00+00:00`       |
| `r`  | RFC 2822 format                      | `Sun, 29 Mar 2026 14:30:00 +0000` |
| `W`  | ISO 8601 week number, leading zero   | `01` to `53`                      |

---

## Number Formatting

### `format_number(value, decimal_places=None)`

Format a number using the `DECIMAL_SEPARATOR`, `THOUSAND_SEPARATOR`, `USE_THOUSAND_SEPARATOR`, and `NUMBER_GROUPING` settings.

```python
from hyperdjango.formats import format_number

format_number(1234567)  # "1,234,567"
format_number(1234567.89)  # "1,234,567.89"
format_number(1234567.89, decimal_places=0)  # "1,234,567"
format_number(1234567.89, decimal_places=4)  # "1,234,567.8900"
format_number("99.5")  # "99.5" (string input)
format_number(-42.1)  # "-42.1"
format_number(float("nan"))  # "NaN"
format_number(float("inf"))  # "Infinity"
```

### `format_currency(value, currency_symbol="$", decimal_places=2)`

Format as currency. Always applies thousand separators regardless of the `USE_THOUSAND_SEPARATOR` setting.

```python
from hyperdjango.formats import format_currency

format_currency(1234.5)  # "$1,234.50"
format_currency(1234.5, currency_symbol="EUR ")  # "EUR 1,234.50"
format_currency(99)  # "$99.00"
format_currency(-42.5)  # "-$42.50"
format_currency(1234.5, decimal_places=0)  # "$1,234"
```

### `format_percent(value, decimal_places=1)`

Format a fraction as a percentage. The value is multiplied by 100.

```python
from hyperdjango.formats import format_percent

format_percent(0.5)  # "50.0%"
format_percent(0.753, decimal_places=2)  # "75.30%"
format_percent(1.0)  # "100.0%"
```

---

## Input Parsing

### `parse_date(value)`

Parse a date string by trying `DATE_INPUT_FORMATS` in order. Returns `None` if no format matches.

```python
from hyperdjango.formats import parse_date

parse_date("2026-03-29")  # date(2026, 3, 29)
parse_date("03/29/2026")  # date(2026, 3, 29)
parse_date("not a date")  # None
```

### `parse_datetime(value)`

Parse a datetime string by trying `DATETIME_INPUT_FORMATS` in order. Returns `None` if no format matches.

```python
from hyperdjango.formats import parse_datetime

parse_datetime("2026-03-29 14:30:00")  # datetime(2026, 3, 29, 14, 30)
parse_datetime("03/29/2026 14:30")  # datetime(2026, 3, 29, 14, 30)
```

---

## Timezone Utilities

### `now()`

Return the current datetime. If `USE_TZ` is `True`, returns a UTC-aware datetime. Otherwise returns a naive datetime.

```python
from hyperdjango.formats import now

dt = now()  # datetime.datetime(2026, 3, 29, 14, 30, 0, tzinfo=datetime.timezone.utc)
```

### `localtime(value, timezone_name=None)`

Convert a datetime to the `TIME_ZONE` setting (or a specific timezone). Naive datetimes are assumed to be UTC.

```python
from hyperdjango.formats import localtime, now

utc_now = now()
eastern = localtime(utc_now, "US/Eastern")
tokyo = localtime(utc_now, "Asia/Tokyo")
default_tz = localtime(utc_now)  # uses TIME_ZONE setting
```

### `make_aware(value, timezone_name=None)`

Attach timezone info to a naive datetime. Raises `ValueError` if the datetime is already timezone-aware.

```python
from hyperdjango.formats import make_aware
from datetime import datetime

naive = datetime(2026, 3, 29, 14, 30)
aware = make_aware(naive, "US/Eastern")
aware_default = make_aware(naive)  # uses TIME_ZONE setting
```

### `make_naive(value, timezone_name=None)`

Strip timezone info, first converting to the target timezone.

```python
from hyperdjango.formats import make_naive
from datetime import datetime, timezone

aware = datetime(2026, 3, 29, 14, 30, tzinfo=timezone.utc)
naive = make_naive(aware, "US/Eastern")  # datetime(2026, 3, 29, 10, 30)
```

### `is_aware(value)` / `is_naive(value)`

Check whether a datetime has timezone info.

```python
from hyperdjango.formats import is_aware, is_naive
from datetime import datetime, timezone

is_aware(datetime(2026, 1, 1, tzinfo=timezone.utc))  # True
is_naive(datetime(2026, 1, 1))  # True
```

---

## Calendar Utilities

### `get_first_day_of_week()`

Return the `FIRST_DAY_OF_WEEK` setting (0 = Sunday, 1 = Monday).

### `get_week_start(dt)`

Return the start date of the week containing `dt`, respecting `FIRST_DAY_OF_WEEK`.

```python
from hyperdjango.formats import get_week_start
from datetime import date

# With FIRST_DAY_OF_WEEK = 1 (Monday):
get_week_start(date(2026, 3, 29))  # date(2026, 3, 23) (Monday)
```

---

## Template Filters

The formats module registers template filters automatically via `Library("formats")`:

| Filter       | Template Usage                                                 | Description                            |
| ------------ | -------------------------------------------------------------- | -------------------------------------- |
| `date`       | `{{ value\|date }}` or `{{ value\|date:"m/d/Y" }}`             | Format a date                          |
| `time`       | `{{ value\|time }}` or `{{ value\|time:"H:i" }}`               | Format a time                          |
| `datetime`   | `{{ value\|datetime }}` or `{{ value\|datetime:"Y-m-d H:i" }}` | Format a datetime                      |
| `number`     | `{{ value\|number }}` or `{{ value\|number:"2" }}`             | Format a number (arg = decimal places) |
| `currency`   | `{{ value\|currency }}` or `{{ value\|currency:"EUR" }}`       | Format as currency (arg = symbol)      |
| `short_date` | `{{ value\|short_date }}`                                      | Format using SHORT_DATE_FORMAT         |

```html
<p>Published: {{ article.created_at|date:"F j, Y" }}</p>
<p>Price: {{ product.price|currency:"$" }}</p>
<p>Rating: {{ score|number:"1" }} out of 5</p>
```

---

## Settings Reference

These settings control formatting behavior. All are configured via `hyperdjango.conf`.

| Setting                  | Default                         | Description                                                                                         |
| ------------------------ | ------------------------------- | --------------------------------------------------------------------------------------------------- |
| `DATE_FORMAT`            | `"N j, Y"`                      | Default date format string                                                                          |
| `DATETIME_FORMAT`        | `"N j, Y, P"`                   | Default datetime format string                                                                      |
| `TIME_FORMAT`            | `"P"`                           | Default time format string                                                                          |
| `SHORT_DATE_FORMAT`      | `"m/d/Y"`                       | Short date format                                                                                   |
| `SHORT_DATETIME_FORMAT`  | `"m/d/Y P"`                     | Short datetime format                                                                               |
| `DECIMAL_SEPARATOR`      | `"."`                           | Character for decimal point                                                                         |
| `THOUSAND_SEPARATOR`     | `","`                           | Character for thousands grouping                                                                    |
| `USE_THOUSAND_SEPARATOR` | `False`                         | Whether to apply thousand separators in `format_number()` (`format_currency()` always applies them) |
| `NUMBER_GROUPING`        | `3`                             | Digits per group                                                                                    |
| `FIRST_DAY_OF_WEEK`      | `0`                             | 0 = Sunday, 1 = Monday                                                                              |
| `USE_TZ`                 | `True`                          | Whether `now()` returns timezone-aware datetimes                                                    |
| `TIME_ZONE`              | `"UTC"`                         | Default timezone for `localtime()`, `make_aware()`                                                  |
| `DATE_INPUT_FORMATS`     | `["%Y-%m-%d", "%m/%d/%Y", ...]` | Formats tried by `parse_date()`                                                                     |
| `DATETIME_INPUT_FORMATS` | `["%Y-%m-%d %H:%M:%S", ...]`    | Formats tried by `parse_datetime()`                                                                 |
| `LANGUAGE_CODE`          | `"en"`                          | Default language code                                                                               |

---

## See Also

- [i18n.md](i18n.md) -- Translation framework (uses `LANGUAGE_CODE`, `LOCALE_PATHS` settings)
- [fields.md](fields.md) -- Custom fields including `MoneyField` and `PercentField` (currency/percentage formatting at the model level)
