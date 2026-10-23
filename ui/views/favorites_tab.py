"""Favorites tab: the View Documents gallery limited to favorite documents."""

from services.documents import DocumentCollection
from views.actions import FavoriteActions
from views.gallery import DocumentGallery
from views.shared import LexiaTab, SearchFilter


class FavoritesTab(LexiaTab):
    def render(self) -> None:
        actions = FavoriteActions(self.api)
        favorites = DocumentCollection.visible(self.api, self.files).only(actions.favorite_names())
        collection = SearchFilter(
            self.layout,
            key="favorite_name_search",
            label="Search favorite documents",
            no_match_message="No favorite documents match your search.",
        ).apply(favorites)
        if collection is not None:
            DocumentGallery(self.context, actions).render(collection)
