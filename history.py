"""Read-only history search and calendar queries. Never load attachment blobs."""

import calendar
import re
from datetime import date


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
    counts = {}
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
        "members": members,
        "records": records,
        "total": total,
        "page": page,
        "pages": pages,
    }
