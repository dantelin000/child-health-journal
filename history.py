"""Read-only history search and calendar queries. Never load attachment blobs."""

import calendar
import colorsys
import re
from datetime import date

CALENDAR_PREVIEWS = 3


def member_color_class(member_id):
    return f"member-color-{int(member_id)}" if member_id is not None else "member-color-legacy"


def member_colors_css(member_ids):
    """Only numeric selectors and generated colors; never put user text in CSS."""
    rules = []
    used = set()
    for member_id in sorted(set(map(int, member_ids))):
        base_hue = ((member_id - 1) * 137508 + 210000) % 360000
        for offset in range(3600):
            hue = ((base_hue + offset * 100) % 360000) / 360000

            def color(lightness, saturation):
                return "#" + "".join(
                    f"{round(channel * 255):02x}"
                    for channel in colorsys.hls_to_rgb(hue, lightness, saturation)
                )

            accent = color(0.36, 0.62)
            if accent not in used:
                break
        used.add(accent)
        rules.append(
            f".{member_color_class(member_id)}{{"
            f"--member-color:{accent};--member-bg:{color(0.94, 0.6)};"
            f"--member-text:{color(0.26, 0.6)};}}"
        )
    return "\n".join(rules) + "\n"


def record_summary(row):
    for key, label in (
        ("symptoms", ""),
        ("care", ""),
        ("medication", "用药："),
        ("temperature", "体温 "),
        ("notes", ""),
    ):
        value = " ".join((row[key] or "").split())
        if value:
            text = label + value
            return text[:59] + "…" if len(text) > 60 else text
    return f"病例单 {row['attachment_count']} 份" if row["attachment_count"] else "健康记录"


def month_details(connection, tables, where, params):
    """Keep previews bounded to three per day, independent of result pagination."""
    details = {}
    marks = connection.execute(
        "SELECT substr(r.occurred,1,10) AS day,r.member_id,"
        "COALESCE(m.name,r.child) AS member_name,count(*) AS count"
        + tables
        + where
        + " GROUP BY day,r.member_id,COALESCE(m.name,r.child) ORDER BY day,r.member_id,member_name",
        params,
    )
    for row in marks:
        item = details.setdefault(row["day"], {"events": [], "marks": []})
        item["marks"].append(
            {
                "name": row["member_name"],
                "count": row["count"],
                "color_class": member_color_class(row["member_id"]),
            }
        )
    previews = connection.execute(
        """SELECT p.*,(SELECT count(*) FROM attachments a WHERE a.record_id=p.id)
        AS attachment_count FROM (
            SELECT r.id,r.member_id,r.occurred,substr(r.occurred,1,10) AS day,
            COALESCE(m.name,r.child) AS member_name,
            substr(r.temperature,1,80) AS temperature,substr(r.symptoms,1,80) AS symptoms,
            substr(r.medication,1,80) AS medication,substr(r.care,1,80) AS care,
            substr(r.notes,1,80) AS notes,
            row_number() OVER (PARTITION BY substr(r.occurred,1,10)
                ORDER BY r.occurred,r.id) AS preview_rank"""
        + tables
        + where
        + ") p WHERE preview_rank<=? ORDER BY occurred,id",
        (*params, CALENDAR_PREVIEWS),
    )
    for row in previews:
        details[row["day"]]["events"].append(
            {
                "id": row["id"],
                "name": row["member_name"],
                "time": row["occurred"][11:16],
                "summary": record_summary(row),
                "color_class": member_color_class(row["member_id"]),
            }
        )
    return details


class HistoryInputError(ValueError):
    """A recoverable filter error that can be shown beside the search form."""


def parse_date(value, label):
    try:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise HistoryInputError(f"请填写有效的{label}。") from None


def month_shift(year, month, offset):
    index = (year - 1) * 12 + month - 1 + offset
    if not 0 <= index < 9999 * 12:
        return None
    year, month = divmod(index, 12)
    return f"{year + 1:04d}-{month + 1:02d}"


def read_history(connection, args, today, page_size=50):
    """Apply the same literal search and family/date filters to every view."""
    q = args.get("q", "").strip()
    member_id = args.get("member", "").strip()
    start, end = (args.get(key, "").strip() for key in ("start", "end"))
    view = args.get("view", "calendar")
    page_text = args.get("page", "1")
    if view not in {"calendar", "list"}:
        raise HistoryInputError("请选择日历或列表查看。")
    if len(q) > 200:
        raise HistoryInputError("搜索关键词不能超过 200 字。")
    if not re.fullmatch(r"[0-9]{1,6}", page_text) or int(page_text) < 1:
        raise HistoryInputError("页码无效。")
    page = int(page_text)
    members = connection.execute("SELECT id,name FROM family_members ORDER BY id").fetchall()
    if member_id and (
        not re.fullmatch(r"[0-9]{1,19}", member_id)
        or int(member_id) not in {member["id"] for member in members}
    ):
        raise HistoryInputError("没有找到所选家人，请重新选择。")
    if start:
        parse_date(start, "开始日期")
    if end:
        parse_date(end, "结束日期")
    if start and end and start > end:
        raise HistoryInputError("开始日期不能晚于结束日期。")

    conditions, params = [], []
    if member_id:
        conditions.append("r.member_id=?")
        params.append(int(member_id))
    if start:
        conditions.append("r.occurred>=?")
        params.append(start + "T00:00")
    if end:
        conditions.append("r.occurred<=?")
        params.append(end + "T23:59:59")
    if q:
        # instr treats percent, underscore and SQL punctuation literally.
        fields = (
            "m.name",
            "m.relationship",
            "r.child",
            "r.temperature",
            "r.symptoms",
            "r.medication",
            "r.care",
            "r.notes",
        )
        matches = [f"instr(lower(COALESCE({field},'')),lower(?))>0" for field in fields]
        matches.append("""EXISTS (SELECT 1 FROM attachments a WHERE a.record_id=r.id
            AND instr(lower(a.filename),lower(?))>0)""")
        conditions.append("(" + " OR ".join(matches) + ")")
        params.extend([q] * len(matches))
    tables = " FROM records r LEFT JOIN family_members m ON m.id=r.member_id"
    where = " WHERE " + " AND ".join(conditions) if conditions else ""

    selected_day = args.get("day", "").strip() if view == "calendar" else ""
    if selected_day:
        parse_date(selected_day, "所选日期")
    month = args.get("month", "").strip()
    if not month:
        month = selected_day[:7] if selected_day else today.isoformat()[:7]
        if not selected_day and any((q, member_id, start, end)):
            latest = connection.execute(
                "SELECT substr(r.occurred,1,7)"
                + tables
                + where
                + " ORDER BY r.occurred DESC,r.id DESC LIMIT 1",
                params,
            ).fetchone()
            month = latest[0] if latest else (start[:7] or end[:7] or month)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise HistoryInputError("请选择有效的月份。")
    parse_date(month + "-01", "月份")
    if selected_day and selected_day[:7] != month:
        raise HistoryInputError("所选日期与月份不一致，请重新选择。")
    year, month_number = map(int, month.split("-"))
    last = calendar.monthrange(year, month_number)[1]
    month_conditions = [*conditions, "r.occurred>=?", "r.occurred<=?"]
    month_params = [*params, month + "-01T00:00", f"{month}-{last:02d}T23:59:59"]
    month_where = " WHERE " + " AND ".join(month_conditions)
    counts, details = {}, {}
    if view == "calendar":
        counts = dict(
            connection.execute(
                "SELECT substr(r.occurred,1,10),count(*)"
                + tables
                + month_where
                + " GROUP BY substr(r.occurred,1,10)",
                month_params,
            ).fetchall()
        )
        details = month_details(connection, tables, month_where, month_params)
        conditions, params = month_conditions, month_params
        if selected_day:
            conditions.extend(["r.occurred>=?", "r.occurred<=?"])
            params.extend([selected_day + "T00:00", selected_day + "T23:59:59"])
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    total = connection.execute("SELECT count(*)" + tables + where, params).fetchone()[0]
    pages = max(1, (total + page_size - 1) // page_size)
    records = connection.execute(
        """SELECT r.*,COALESCE(m.name,r.child) AS member_name,
        (SELECT count(*) FROM attachments a WHERE a.record_id=r.id) AS attachment_count"""
        + tables
        + where
        + " ORDER BY r.occurred DESC,r.id DESC LIMIT ? OFFSET ?",
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdayscalendar(year, month_number):
        days = []
        for day in week:
            day_date = f"{month}-{day:02d}" if day else ""
            days.append(
                {
                    "number": day,
                    "date": day_date,
                    "count": counts.get(day_date, 0),
                    "today": day_date == today.isoformat(),
                    "selected": day_date == selected_day,
                    "events": details.get(day_date, {}).get("events", []),
                    "family_marks": details.get(day_date, {}).get("marks", [])[:4],
                    "extra_families": max(0, len(details.get(day_date, {}).get("marks", [])) - 4),
                    "more": max(0, counts.get(day_date, 0) - CALENDAR_PREVIEWS),
                    "more_mobile": max(0, counts.get(day_date, 0) - 2),
                }
            )
        weeks.append(days)
    return {
        "filters": {"q": q, "member": member_id, "start": start, "end": end},
        "view": view,
        "month": month,
        "month_title": f"{year} 年 {month_number} 月",
        "today_month": today.isoformat()[:7],
        "day": selected_day,
        "previous_month": month_shift(year, month_number, -1),
        "next_month": month_shift(year, month_number, 1),
        "weeks": weeks,
        "month_total": sum(counts.values()),
        "has_legacy_events": any(
            mark["color_class"] == "member-color-legacy"
            for value in details.values()
            for mark in value["marks"]
        ),
        "members": [
            {**dict(member), "color_class": member_color_class(member["id"])} for member in members
        ],
        "records": records,
        "total": total,
        "page": page,
        "pages": pages,
    }
