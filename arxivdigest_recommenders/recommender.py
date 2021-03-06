import asyncio
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Sequence, Optional
from arxivdigest.connector import ArxivdigestConnector

from arxivdigest_recommenders import config
from arxivdigest_recommenders.semantic_scholar import SemanticScholar
from arxivdigest_recommenders.util import extract_s2_id, chunks
from arxivdigest_recommenders.log import get_logger


class ArxivdigestRecommender(ABC):
    """Base class for arXivDigest recommender systems."""

    def __init__(self, arxivdigest_api_key: str, name: str):
        self._arxivdigest_api_key = arxivdigest_api_key
        self._logger = get_logger(name, name)

    @abstractmethod
    async def score_paper(
        self, user: dict, user_s2_id: str, paper_id: str
    ) -> Optional[Dict[str, Any]]:
        """Score a paper for a user.

        If the paper for some reason cannot be scored (e.g., if there's not enough data available or because the paper
        is authored by the user), nothing (or None) should be returned.

        :param user: User data.
        :param user_s2_id: S2 author ID of the user.
        :param paper_id: arXiv ID of paper.
        :return: Dictionary containing article_id, explanation, and score keys.
        """
        pass

    async def user_ranking(
        self, user: dict, user_s2_id: str, paper_ids: Sequence[str], batch_size=10
    ) -> List[Dict[str, Any]]:
        """Generate ranking of papers for a user.

        :param user: User data.
        :param user_s2_id: S2 author ID of the user.
        :param paper_ids: arXiv IDs of papers.
        :param batch_size: Number of papers scored concurrently.
        :return: Ranking of candidate papers.
        """
        results = []
        for paper_id_chunk in chunks(paper_ids, 5):
            chunk_results = await asyncio.gather(
                *[self.score_paper(user, user_s2_id, p) for p in paper_id_chunk],
                return_exceptions=True
            )
            results.extend(
                r for r in chunk_results if isinstance(r, dict) and r["score"] > 0
            )
        return results

    async def recommendations(
        self,
        users: dict,
        interleaved_papers: dict,
        paper_ids: Sequence[str],
        max_recommendations=10,
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Generate recommendations for a user batch.

        :param users: Users.
        :param interleaved_papers: Interleaved papers that will be excluded from the generated recommendations
        before submission.
        :param paper_ids: arXiv IDs of candidate papers.
        :param max_recommendations: Max number of recommendations per user.
        :return: Recommendations.
        """
        recommendations = {}
        for user_id, user_data in users.items():
            s2_id = extract_s2_id(user_data)
            if s2_id is None:
                self._logger.info("User %s: skipped (no S2 ID provided).", user_id)
                continue
            try:
                # Validate the user's S2 ID.
                async with SemanticScholar() as s2:
                    await s2.author(s2_id)
            except Exception:
                self._logger.error(
                    "User %: unable to get author details for S2 ID %s.", user_id, s2_id
                )
                continue
            user_ranking = [
                r
                for r in await self.user_ranking(user_data, s2_id, paper_ids)
                if r["article_id"] not in interleaved_papers[user_id]
            ]
            user_recommendations = sorted(
                user_ranking, key=lambda r: r["score"], reverse=True
            )[:max_recommendations]
            self._logger.info(
                "User %s: recommended %d papers.", user_id, len(user_recommendations)
            )
            recommendations[user_id] = user_recommendations
        return {
            user_id: user_recommendations
            for user_id, user_recommendations in recommendations.items()
            if len(user_recommendations) > 0
        }

    async def recommend(
        self, submit_recommendations=True
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Generate and submit recommendations for all users.

        :param submit_recommendations: Submit recommendations to arXivDigest.
        :return: Recommendations.
        """
        connector = ArxivdigestConnector(
            self._arxivdigest_api_key, config.ARXIVDIGEST_BASE_URL
        )
        paper_ids = connector.get_article_ids()
        total_users = connector.get_number_of_users()
        self._logger.info(
            "%d candidate papers and %d users.", len(paper_ids), total_users
        )
        recommendation_count = 0
        recommendations = {}
        while recommendation_count < total_users:
            user_ids = connector.get_user_ids(recommendation_count)
            users = connector.get_user_info(user_ids)
            interleaved = connector.get_interleaved_articles(user_ids)
            batch_recommendations = await self.recommendations(
                users, interleaved, paper_ids
            )
            recommendations.update(batch_recommendations)
            if batch_recommendations and submit_recommendations:
                connector.send_article_recommendations(batch_recommendations)
            recommendation_count += len(user_ids)
            self._logger.info("Processed %d users.", recommendation_count)
        self._logger.info("Finished recommending.")
        self._logger.info(
            "Semantic Scholar API: %d cache hits, %d cache misses, %d requests, and %d errors.",
            SemanticScholar.cache_hits,
            SemanticScholar.cache_misses,
            SemanticScholar.requests,
            SemanticScholar.errors,
        )
        return recommendations
