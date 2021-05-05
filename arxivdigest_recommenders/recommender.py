import asyncio
from abc import ABC, abstractmethod
from datetime import date
from collections import defaultdict
from typing import List, Dict, Any, Optional
from arxivdigest.connector import ArxivdigestConnector

from arxivdigest_recommenders import config
from arxivdigest_recommenders.semantic_scholar import SemanticScholar
from arxivdigest_recommenders.util import (
    extract_s2_id,
    padded_cosine_sim,
    gather,
)
from arxivdigest_recommenders.log import logger


class ArxivdigestRecommender(ABC):
    """Base class for arXivDigest recommender systems."""

    def __init__(self, arxivdigest_api_key: str):
        self._arxivdigest_api_key = arxivdigest_api_key
        self._connector: Optional[ArxivdigestConnector] = None

        # Stores the vector representations of the papers (their authors) that are candidate for recommendation.
        self._papers: Dict[str, List[Dict[str, Any]]] = {}

        # Used as a cache for the vector representations of authors.
        self._authors: Dict[str, List[int]] = {}

        # Locks used to ensure that the vector representation of an author is created only once if missing.
        self._author_locks = defaultdict(asyncio.Lock)

    @abstractmethod
    def author_representation(
        self, author: dict, published_papers: List[dict]
    ) -> List[int]:
        """Generate vector representation of an author based on the author's details and published papers.

        :param author: S2 author details.
        :param published_papers: S2 paper details for the author's published papers.
        """
        pass

    async def get_author_representation(self, s2_id: str) -> List[int]:
        """Get the vector representation of an author.

        The returned vector is N-dimensional, where N is the number of venues that have been discovered by the
        recommender thus far. Each value in the vector corresponds to a certain venue and represents the number of
        times the author has published there.

        :param s2_id: S2 author ID.
        :return: Author vector representation.
        """
        async with self._author_locks[s2_id]:
            if s2_id not in self._authors:
                year_cutoff = date.today().year - config.MAX_PAPER_AGE
                async with SemanticScholar() as s2:
                    author = await s2.author(s2_id)
                    papers = await gather(
                        *[
                            s2.paper(s2_id=paper["paperId"])
                            for paper in author["papers"]
                            if paper["year"] is None or paper["year"] >= year_cutoff
                        ]
                    )
                self._authors[s2_id] = self.author_representation(author, papers)
        return self._authors[s2_id]

    async def _gen_paper_representations(self):
        """Generate author representations for each author of each paper that is candidate for recommendation."""

        async def author_representations(paper: dict):
            authors = await gather(
                *[
                    self.get_author_representation(author["authorId"])
                    for author in paper["authors"]
                    if author["authorId"]
                ]
            )
            return [
                {"name": paper["authors"][i]["name"], "representation": author}
                for i, author in enumerate(authors)
            ]

        paper_ids = self._connector.get_article_ids()
        logger.info(
            f"Generating vector representations of {len(paper_ids)} candidate papers and their authors."
        )
        async with SemanticScholar() as s2:
            papers = await gather(
                *[s2.paper(arxiv_id=paper_id) for paper_id in paper_ids]
            )
        paper_authors = await asyncio.gather(
            *[author_representations(paper) for paper in papers]
        )
        self._papers = {
            paper["arxivId"]: authors for paper, authors in zip(papers, paper_authors)
        }

    @abstractmethod
    def explanation(self, user: List[int], author: List[int], author_name: str) -> str:
        """Generate a recommendation explanation based on the vector representations of a user and an author.

        :param user: User vector representation.
        :param author: Author vector representation.
        :param author_name: Author name.
        :return: Explanation.
        """
        pass

    async def user_ranking(self, s2_id: str) -> List[Dict[str, Any]]:
        """Generate paper ranking for a single user.

        The score assigned to each paper is the cosine similarity between the user and the paper author that is
        most similar to the user.

        :param s2_id: S2 author ID of the user.
        :return: Ranking of candidate papers.
        """
        try:
            user = await self.get_author_representation(s2_id)
        except Exception:
            logger.error(f"S2 ID {s2_id}: unable to generate author representation.")
            return []
        results = []
        for paper_id, authors in self._papers.items():
            similar_author, score = max(
                [
                    (author, padded_cosine_sim(user, author["representation"]))
                    for author in authors
                ],
                key=lambda t: t[1],
            )
            results.append(
                {
                    "article_id": paper_id,
                    "score": score,
                    "explanation": self.explanation(
                        user, similar_author["representation"], similar_author["name"]
                    )
                    if score > 0
                    else "",
                }
            )
        return results

    async def recommendations(
        self, users: dict, interleaved_papers: dict, max_recommendations=10
    ) -> Dict[str, Dict[str, Any]]:
        """Generate recommendations for a user batch.

        :param users: Users.
        :param interleaved_papers: Interleaved papers that will be excluded from the generated recommendations
        before submission.
        :param max_recommendations: Max number of recommendations per user.
        :return: Recommendations.
        """

        async def user_recommendations(user_id: str, user_data: dict):
            s2_id = extract_s2_id(user_data)
            if s2_id is None:
                logger.info(f"User {user_id}: skipped (no S2 ID provided).")
                return []
            ranking = await self.user_ranking(s2_id)
            ranking = [
                recommendation
                for recommendation in ranking
                if recommendation["article_id"] not in interleaved_papers[user_id]
                and recommendation["score"] > 0
            ]
            recommendations = sorted(ranking, key=lambda r: r["score"], reverse=True)[
                :max_recommendations
            ]
            logger.info(f"User {user_id}: recommended {len(recommendations)} papers.")
            return recommendations

        recommendations = await asyncio.gather(
            *[
                user_recommendations(user_id, user_data)
                for user_id, user_data in users.items()
            ]
        )
        return {
            user_id: user_recommendations
            for user_id, user_recommendations in zip(users.keys(), recommendations)
            if len(user_recommendations) > 0
        }

    async def recommend(self):
        """Generate and submit recommendations for all users."""
        if self._connector is None:
            self._connector = ArxivdigestConnector(
                self._arxivdigest_api_key, config.ARXIVDIGEST_BASE_URL
            )
        total_users = self._connector.get_number_of_users()
        logger.info(f"Recommending papers for {total_users} users.")
        await self._gen_paper_representations()
        recommendation_count = 0
        while recommendation_count < total_users:
            user_ids = self._connector.get_user_ids(recommendation_count)
            users = self._connector.get_user_info(user_ids)
            interleaved = self._connector.get_interleaved_articles(user_ids)

            recommendations = await self.recommendations(users, interleaved)

            if recommendations:
                self._connector.send_article_recommendations(recommendations)
            recommendation_count += len(user_ids)
            logger.info(f"Processed {recommendation_count} users.")
        logger.info("Finished recommending.")
