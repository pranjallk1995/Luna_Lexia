"""View Documents tab: the searchable visible-document gallery."""

from services.documents import DocumentCollection
from views.actions import GalleryActions
from views.gallery import DocumentGallery
from views.shared import LexiaTab, SearchFilter


class ViewTab(LexiaTab):
    def render(self) -> None:
        collection = SearchFilter(
            self.layout,
            key="view_name_search",
            label="Search documents",
            no_match_message="No documents match your search.",
        ).apply(DocumentCollection.visible(self.api, self.files))
        if collection is not None:
            DocumentGallery(self.context, GalleryActions(self.api)).render(collection)
