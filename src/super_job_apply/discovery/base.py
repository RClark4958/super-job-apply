"""Abstract base class for job discovery sources."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import JobPosting, SearchCriteria


class JobSource(ABC):
    """Base class for all job discovery sources."""

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Identifier for this source (e.g. 'exa_company', 'exa_jobs')."""
        ...

    @abstractmethod
    async def discover(self, criteria: SearchCriteria) -> list[JobPosting]:
        """Discover job postings matching the given criteria.

        Args:
            criteria: Search parameters (queries, locations, filters).

        Returns:
            List of discovered JobPosting objects.
        """
        ...
