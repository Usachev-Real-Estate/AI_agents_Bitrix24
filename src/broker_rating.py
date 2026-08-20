"""Broker CRM rating engine: composite 0–100% score from 4 components."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Settings, get_settings
from db import (
    count_pool_deals_for_broker,
    count_shared_leads_for_broker,
    get_broker_daily_metrics_summary,
    get_violations_for_broker,
    save_broker_ratings_snapshot,
)

logger = logging.getLogger(__name__)

ACTIVE_TASK_STATUSES = {2, 3, 4}
TASK_CLOSED_STATUS = 5


@dataclass
class TasksMetrics:
    overdue: int = 0
    active_with_deadline: int = 0
    closed_on_time: int = 0
    total_tasks: int = 0
    had_deadline_tasks: bool = False
    all_closed_on_time: bool = False


@dataclass
class EngagementMetrics:
    crm_visit_days: int = 0
    workdays_in_period: int = 0
    important_posts_total: int = 0
    important_posts_read: int = 0


@dataclass
class BrokerRating:
    responsible_id: int
    responsible_name: str
    department: str
    department_id: int | None
    score: float
    tier: str
    tier_label: str
    crm_score: float
    portfolio_score: float
    tasks_score: float
    engagement_score: float
    clean_day_bonus: float
    violations_count: int = 0
    shared_leads_count: int = 0
    pool_deals_count: int = 0
    overdue_tasks: int = 0
    tasks_closed_on_time: int = 0
    crm_visit_days: int = 0
    news_read_ratio: float = 0.0
    clean_days: int = 0
    lead_count: int = 0
    deal_count: int = 0
    task_count: int = 0
    rank_overall: int | None = None
    rank_in_dept: int | None = None
    violations_by_rule: dict[str, int] = field(default_factory=dict)


def count_workdays(since: datetime, until: datetime) -> int:
    """Count Mon–Fri days in [since, until)."""
    count = 0
    day = since.date()
    end = until.date()
    while day < end:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return max(count, 1)


def _severity_penalty(severity: str, weights: dict[str, Any]) -> float:
    penalties = weights.get("severity_penalties") or {}
    key = (severity or "medium").strip().lower()
    return float(penalties.get(key, penalties.get("medium", 4)))


def compute_crm_score(
    violations: list[dict[str, Any]],
    weights: dict[str, Any] | None = None,
    portfolio_size: int = 0,
) -> tuple[float, int, dict[str, int]]:
    """Return (crm_score 0–100, count, by_rule).

    Штраф смягчается при большой базе лидов/сделок (нормализация по нагрузке).
    """
    w = weights or get_settings().broker_rating_weights
    penalty = 0.0
    by_rule: dict[str, int] = defaultdict(int)
    for v in violations:
        by_rule[str(v.get("rule") or "")] += 1
        penalty += _severity_penalty(str(v.get("severity") or ""), w)

    v_count = len(violations)
    if portfolio_size >= 5 and v_count > 0:
        penalty *= min(1.0, (v_count / portfolio_size) * 2)

    penalty = min(penalty, 100.0)
    return max(0.0, 100.0 - penalty), v_count, dict(by_rule)


def broker_has_active_crm(broker: dict[str, Any]) -> bool:
    """True if broker has at least one assigned lead or deal."""
    leads = int(broker.get("lead_count") or 0)
    deals = int(broker.get("deal_count") or 0)
    return leads > 0 or deals > 0


def compute_portfolio_score(
    shared_leads: int,
    pool_deals: int,
    weights: dict[str, Any] | None = None,
) -> float:
    w = weights or get_settings().broker_rating_weights
    shared_pen = float(w.get("shared_lead_penalty", 12))
    pool_pen = float(w.get("pool_deal_penalty", 18))
    penalty = min(100.0, shared_leads * shared_pen + pool_deals * pool_pen)
    return max(0.0, 100.0 - penalty)


def compute_tasks_score(
    metrics: TasksMetrics,
    weights: dict[str, Any] | None = None,
) -> float:
    w = weights or get_settings().broker_rating_weights
    if not metrics.had_deadline_tasks:
        return float(w.get("tasks_neutral_score", 75))
    active = max(metrics.active_with_deadline, 1)
    overdue_part = max(0.0, 100.0 * (1.0 - metrics.overdue / active))
    closure_bonus = (
        float(w.get("tasks_closure_bonus", 5))
        if metrics.all_closed_on_time
        else 0.0
    )
    return min(100.0, overdue_part + closure_bonus)


def compute_engagement_score(metrics: EngagementMetrics) -> float:
    workdays = max(metrics.workdays_in_period, 1)
    crm_part = min(100.0, 100.0 * metrics.crm_visit_days / workdays)
    if metrics.important_posts_total <= 0:
        news_part = 100.0
    else:
        news_part = min(
            100.0,
            100.0 * metrics.important_posts_read / metrics.important_posts_total,
        )
    return 0.5 * crm_part + 0.5 * news_part


def compute_clean_day_bonus(
    clean_days: int,
    weights: dict[str, Any] | None = None,
) -> float:
    w = weights or get_settings().broker_rating_weights
    per_day = float(w.get("clean_day_bonus", 0.5))
    cap = float(w.get("clean_day_bonus_max", 5))
    return min(cap, clean_days * per_day)


def score_to_tier(score: float, weights: dict[str, Any] | None = None) -> tuple[str, str]:
    w = weights or get_settings().broker_rating_weights
    green = float(w.get("tier_green", 80))
    yellow = float(w.get("tier_yellow", 50))
    if score >= green:
        return "green", "🟢 ОТЛИЧНО"
    if score >= yellow:
        return "yellow", "🟡 СРЕДНЕ"
    return "red", "🔴 ПЛОХО"


def compute_total_score(
    crm_score: float,
    portfolio_score: float,
    tasks_score: float,
    engagement_score: float,
    clean_day_bonus: float,
    weights: dict[str, Any] | None = None,
) -> float:
    w = weights or get_settings().broker_rating_weights
    cw = w.get("component_weights") or {}
    total = (
        float(cw.get("crm", 0.60)) * crm_score
        + float(cw.get("portfolio", 0.20)) * portfolio_score
        + float(cw.get("tasks", 0.10)) * tasks_score
        + float(cw.get("engagement", 0.10)) * engagement_score
        + clean_day_bonus
    )
    return min(100.0, max(0.0, total))


def analyze_tasks(tasks: list[dict[str, Any]], now: datetime) -> TasksMetrics:
    """Classify broker tasks for rating."""
    metrics = TasksMetrics(total_tasks=len(tasks))
    for task in tasks:
        status = int(task.get("status") or task.get("STATUS") or 0)
        deadline_raw = task.get("deadline") or task.get("DEADLINE") or ""
        if not deadline_raw:
            continue
        metrics.had_deadline_tasks = True
        try:
            text = str(deadline_raw)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            deadline = datetime.fromisoformat(text)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        if status in ACTIVE_TASK_STATUSES:
            metrics.active_with_deadline += 1
            if deadline < now:
                metrics.overdue += 1
        elif status == TASK_CLOSED_STATUS and deadline >= now:
            metrics.closed_on_time += 1

    metrics.all_closed_on_time = (
        metrics.had_deadline_tasks
        and metrics.overdue == 0
        and metrics.active_with_deadline == 0
    )
    return metrics


def build_engagement_metrics(
    broker_id: int,
    since: str,
    until: str,
    since_dt: datetime,
    until_dt: datetime,
    important_posts: list[dict[str, Any]] | None = None,
) -> EngagementMetrics:
    """Build engagement metrics from daily DB + optional feed data."""
    daily = get_broker_daily_metrics_summary(broker_id, since, until)
    posts = important_posts or []
    read_count = 0
    for post in posts:
        readers = {int(x) for x in (post.get("readers") or [])}
        if broker_id in readers:
            read_count += 1

    return EngagementMetrics(
        crm_visit_days=int(daily.get("crm_visit_days") or 0),
        workdays_in_period=count_workdays(since_dt, until_dt),
        important_posts_total=len(posts),
        important_posts_read=read_count,
    )


def compute_broker_rating(
    broker: dict[str, Any],
    since: str,
    until: str,
    *,
    tasks: list[dict[str, Any]] | None = None,
    important_posts: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
) -> BrokerRating:
    """Compute full rating for one broker."""
    cfg = settings or get_settings()
    weights = cfg.broker_rating_weights
    broker_id = int(broker["responsible_id"])
    since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    until_dt = datetime.fromisoformat(until.replace("Z", "+00:00"))
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=timezone.utc)

    violations = get_violations_for_broker(broker_id, since, until)
    lead_count = int(broker.get("lead_count") or 0)
    deal_count = int(broker.get("deal_count") or 0)
    portfolio_size = lead_count + deal_count

    crm_score, v_count, by_rule = compute_crm_score(
        violations, weights, portfolio_size=portfolio_size,
    )

    shared = count_shared_leads_for_broker(broker_id, since, until)
    pool = count_pool_deals_for_broker(broker_id, since, until)
    portfolio_score = compute_portfolio_score(shared, pool, weights)

    task_metrics = analyze_tasks(tasks or [], until_dt)
    tasks_score = compute_tasks_score(task_metrics, weights)

    engagement = build_engagement_metrics(
        broker_id, since, until, since_dt, until_dt, important_posts,
    )
    engagement_score = compute_engagement_score(engagement)

    daily = get_broker_daily_metrics_summary(broker_id, since, until)
    clean_days = int(daily.get("clean_days") or 0)
    clean_bonus = compute_clean_day_bonus(clean_days, weights)

    score = compute_total_score(
        crm_score, portfolio_score, tasks_score, engagement_score, clean_bonus, weights,
    )
    tier, tier_label = score_to_tier(score, weights)

    news_ratio = (
        engagement.important_posts_read / engagement.important_posts_total
        if engagement.important_posts_total > 0
        else 1.0
    )

    return BrokerRating(
        responsible_id=broker_id,
        responsible_name=str(broker.get("responsible_name") or f"ID {broker_id}"),
        department=str(broker.get("department") or ""),
        department_id=broker.get("department_id"),
        score=round(score, 1),
        tier=tier,
        tier_label=tier_label,
        crm_score=round(crm_score, 1),
        portfolio_score=round(portfolio_score, 1),
        tasks_score=round(tasks_score, 1),
        engagement_score=round(engagement_score, 1),
        clean_day_bonus=round(clean_bonus, 1),
        violations_count=v_count,
        shared_leads_count=shared,
        pool_deals_count=pool,
        overdue_tasks=task_metrics.overdue,
        tasks_closed_on_time=task_metrics.closed_on_time,
        crm_visit_days=engagement.crm_visit_days,
        news_read_ratio=round(news_ratio, 2),
        clean_days=clean_days,
        lead_count=lead_count,
        deal_count=deal_count,
        task_count=task_metrics.total_tasks,
        violations_by_rule=by_rule,
    )


def assign_ranks(ratings: list[BrokerRating]) -> None:
    """Set rank_overall and rank_in_dept in place."""
    sorted_all = sorted(ratings, key=lambda r: (-r.score, r.responsible_name))
    for i, r in enumerate(sorted_all, 1):
        r.rank_overall = i

    by_dept: dict[str, list[BrokerRating]] = defaultdict(list)
    for r in ratings:
        by_dept[r.department or ""].append(r)
    for dept_ratings in by_dept.values():
        dept_sorted = sorted(dept_ratings, key=lambda r: (-r.score, r.responsible_name))
        for i, r in enumerate(dept_sorted, 1):
            r.rank_in_dept = i


def rating_to_dict(r: BrokerRating) -> dict[str, Any]:
    return {
        "responsible_id": r.responsible_id,
        "responsible_name": r.responsible_name,
        "department": r.department,
        "department_id": r.department_id,
        "score": r.score,
        "tier": r.tier,
        "tier_label": r.tier_label,
        "crm_score": r.crm_score,
        "portfolio_score": r.portfolio_score,
        "tasks_score": r.tasks_score,
        "engagement_score": r.engagement_score,
        "violations_count": r.violations_count,
        "shared_leads_count": r.shared_leads_count,
        "pool_deals_count": r.pool_deals_count,
        "overdue_tasks": r.overdue_tasks,
        "tasks_closed_on_time": r.tasks_closed_on_time,
        "crm_visit_days": r.crm_visit_days,
        "news_read_ratio": r.news_read_ratio,
        "clean_days": r.clean_days,
        "rank_overall": r.rank_overall,
        "rank_in_dept": r.rank_in_dept,
    }


def format_rating_leaderboard(
    ratings: list[BrokerRating],
    since: datetime,
    until: datetime,
    prev_scores: dict[int, float] | None = None,
    dept_name: str | None = None,
    top_n: int | None = None,
    full_list: bool = True,
) -> str:
    """Format rating table for chat."""
    cfg = get_settings()
    n = top_n or cfg.broker_rating_top_n
    prev = prev_scores or {}

    has_fixed_since = bool((cfg.broker_rating_since or "").strip())
    if has_fixed_since:
        title = "b24-ai-auditor — Рейтинг CRM"
    elif cfg.broker_rating_period_days == 7:
        title = "b24-ai-auditor — Недельный рейтинг CRM"
    else:
        title = "b24-ai-auditor — Рейтинг CRM"
    if dept_name:
        title += f" (Отдел: {dept_name})"

    start_s = since.strftime("%d.%m.%Y")
    end_s = until.strftime("%d.%m.%Y")
    avg = sum(r.score for r in ratings) / len(ratings) if ratings else 0.0

    lines = [
        title,
        f"Период: {start_s} – {end_s} ({_rating_period_label(since, cfg)})",
        "",
        "═══════════════════════════════",
        f"📊 РЕЙТИНГ ({len(ratings)} брокеров, средний балл {avg:.0f}%)",
        "═══════════════════════════════",
        "",
    ]

    sorted_ratings = sorted(ratings, key=lambda r: r.rank_overall or 999)

    def _format_row(r: BrokerRating) -> None:
        delta = ""
        if r.responsible_id in prev:
            d = r.score - prev[r.responsible_id]
            if d > 0:
                delta = f" (+{d:.0f})"
            elif d < 0:
                delta = f" ({d:.0f})"
        lines.append(
            f" {r.rank_overall}. {r.responsible_name} — "
            f"{r.score:.0f}% {r.tier_label}{delta}"
        )

    if full_list:
        lines.append("📋 ПОЛНЫЙ РЕЙТИНГ")
        lines.append("(только брокеры с активными лидами или сделками)")
        lines.append("")
        for r in sorted_ratings:
            _format_row(r)
        lines.append("")
    else:
        top = sorted_ratings[:n]
        if top:
            lines.append(f"🏆 ТОП-{len(top)}")
            for r in top:
                _format_row(r)
            lines.append("")

        if len(sorted_ratings) > 3:
            lines.append("📉 Нижние 3")
            for r in sorted_ratings[-3:]:
                lines.append(
                    f" {r.rank_overall}. {r.responsible_name} — "
                    f"{r.score:.0f}% {r.tier_label}"
                )
            lines.append("")

    return "\n".join(lines)


def format_rating_scorecard(rating: BrokerRating, since: datetime, until: datetime) -> str:
    """Format personal broker rating (итоговый процент)."""
    lines = [
        "b24-ai-auditor — Рейтинг брокера",
        f"Дата: {until.strftime('%d.%m.%Y')}",
        f"Период: {since.strftime('%d.%m.%Y')} – {until.strftime('%d.%m.%Y')}",
        "",
        f"👤 {rating.responsible_name}",
        "═══════════════════════════════",
        "",
        f"📊 Рейтинг: {rating.score:.0f}% — {rating.tier_label}",
    ]
    if rating.rank_overall:
        rank_dept = f", #{rating.rank_in_dept} в отделе" if rating.rank_in_dept else ""
        lines.append(f"   Место: #{rating.rank_overall} в общем рейтинге{rank_dept}")
    lines.extend([
        "",
        f"📋 База CRM: {rating.lead_count} лидов, {rating.deal_count} сделок, "
        f"{rating.task_count} задач",
        "",
        "═══════════════════════════════",
    ])
    return "\n".join(lines)


def persist_ratings_snapshot(ratings: list[BrokerRating], snapshot_date: str) -> None:
    """Save ratings to DB."""
    save_broker_ratings_snapshot([rating_to_dict(r) for r in ratings], snapshot_date)


def compute_all_ratings(
    since: str | None = None,
    until: str | None = None,
    *,
    brokers: list[dict[str, Any]] | None = None,
    tasks_by_broker: dict[int, list[dict[str, Any]]] | None = None,
    important_posts: list[dict[str, Any]] | None = None,
    settings: Settings | None = None,
) -> list[BrokerRating]:
    """Compute ratings for all active brokers."""
    cfg = settings or get_settings()
    now = datetime.now(timezone.utc)
    until_iso = until or now.isoformat()
    since_dt = now - timedelta(days=cfg.broker_rating_period_days)
    since_iso = since or since_dt.isoformat()

    broker_list = brokers
    if broker_list is None:
        from broker_rating_collectors import list_eligible_brokers_for_rating
        broker_list = list_eligible_brokers_for_rating(cfg)
    ratings: list[BrokerRating] = []
    for broker in broker_list:
        if not broker_has_active_crm(broker):
            continue
        bid = int(broker["responsible_id"])
        tasks = (tasks_by_broker or {}).get(bid, [])
        rating = compute_broker_rating(
            broker,
            since_iso,
            until_iso,
            tasks=tasks,
            important_posts=important_posts,
            settings=cfg,
        )
        ratings.append(rating)

    assign_ranks(ratings)
    return ratings


def _rating_period_label(since: datetime, cfg: Settings | None = None) -> str:
    settings = cfg or get_settings()
    if (settings.broker_rating_since or "").strip():
        return f"с {since.strftime('%d.%m.%Y')}"
    if settings.broker_rating_period_days == 7:
        return "текущая неделя"
    return f"за {settings.broker_rating_period_days} дней"


def get_rating_period() -> tuple[str, str, datetime, datetime]:
    """Return (since_iso, until_iso, since_dt, until_dt).

    Priority:
    1. BROKER_RATING_SINCE (YYYY-MM-DD) — accumulate from that day 00:00 MSK
    2. period_days == 7 — calendar week Mon 00:00 UTC → now
    3. otherwise — rolling window of period_days
    """
    from zoneinfo import ZoneInfo

    cfg = get_settings()
    until_dt = datetime.now(timezone.utc)
    since_raw = (cfg.broker_rating_since or "").strip()
    if since_raw:
        try:
            day = datetime.strptime(since_raw[:10], "%Y-%m-%d").date()
            since_local = datetime(
                day.year, day.month, day.day, 0, 0, 0,
                tzinfo=ZoneInfo("Europe/Moscow"),
            )
            since_dt = since_local.astimezone(timezone.utc)
        except ValueError:
            logger.warning("Invalid BROKER_RATING_SINCE=%r — fallback to period_days", since_raw)
            since_dt = until_dt - timedelta(days=cfg.broker_rating_period_days)
    elif cfg.broker_rating_period_days == 7:
        since_dt = (until_dt - timedelta(days=until_dt.weekday())).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    else:
        since_dt = until_dt - timedelta(days=cfg.broker_rating_period_days)
    return since_dt.isoformat(), until_dt.isoformat(), since_dt, until_dt
