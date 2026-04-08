from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select

from services.storage.models import AnalyticsEvent, NON_DOWNLOAD_ACTIONS, StatsSnapshot


class AnalyticsRepositoryMixin:
    @staticmethod
    def _stats_period_start(period: str) -> datetime:
        start_date = datetime.now()
        if period == "Week":
            start_date -= timedelta(weeks=1)
        elif period == "Month":
            start_date -= timedelta(days=30)
        elif period == "Year":
            start_date -= timedelta(days=365)
        return start_date

    @staticmethod
    def _normalize_stats_date(date_val) -> str:
        if isinstance(date_val, str):
            return datetime.strptime(date_val, "%Y-%m-%d").strftime("%Y-%m-%d")
        return date_val.strftime("%Y-%m-%d")

    async def get_download_stats(self, period: str) -> StatsSnapshot:
        async with self.SessionLocal() as session:
            start_date = self._stats_period_start(period)
            result = await session.execute(
                select(func.date(AnalyticsEvent.created_at), AnalyticsEvent.action_name, func.count())
                .where(
                    AnalyticsEvent.created_at >= start_date,
                    AnalyticsEvent.action_name.notin_(NON_DOWNLOAD_ACTIONS),
                )
                .group_by(func.date(AnalyticsEvent.created_at), AnalyticsEvent.action_name)
                .order_by(func.date(AnalyticsEvent.created_at))
            )

            totals_by_date: dict[str, int] = defaultdict(int)
            by_service: dict[str, dict[str, int]] = defaultdict(dict)
            service_totals: dict[str, int] = defaultdict(int)
            total_downloads = 0

            for date_val, action_name, count in result.all():
                normalized = self._normalize_stats_date(date_val)
                service = self._map_action_to_service(action_name)

                totals_by_date[normalized] += count
                by_service[service][normalized] = by_service[service].get(normalized, 0) + count
                service_totals[service] += count
                total_downloads += count

            return StatsSnapshot(
                totals_by_date=dict(totals_by_date),
                by_service={service: dict(values) for service, values in by_service.items()},
                service_totals=dict(service_totals),
                total_downloads=total_downloads,
            )

    async def get_downloaded_files_count(self, period: str):
        snapshot = await self.get_download_stats(period)
        return snapshot.totals_by_date

    @staticmethod
    def _map_action_to_service(action_name: str) -> str:
        lower = (action_name or "").lower()
        if "tiktok" in lower:
            return "TikTok"
        if "instagram" in lower:
            return "Instagram"
        if "youtube" in lower:
            return "YouTube"
        if "soundcloud" in lower:
            return "SoundCloud"
        if "pinterest" in lower:
            return "Pinterest"
        if "twitter" in lower or "x_" in lower:
            return "Twitter"
        return "Other"

    async def get_downloaded_files_by_service(self, period: str) -> dict[str, dict[str, int]]:
        snapshot = await self.get_download_stats(period)
        return snapshot.by_service
