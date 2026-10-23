"""Hidden Documents tab for the OAuth-authenticated Luna Lexia user."""

from services.documents import DocumentCollection
from views.actions import HiddenActions
from views.gallery import DocumentGallery
from views.shared import LexiaTab, SearchFilter


class HiddenTab(LexiaTab):
    def render(self) -> None:
        collection = SearchFilter(
            self.layout,
            key="hidden_name_search",
            label="Search hidden documents",
            no_match_message="No hidden documents match your search.",
        ).apply(DocumentCollection.from_metadata(self.files, self.api.hidden_documents()))
        if collection is not None:
            DocumentGallery(self.context, HiddenActions(self.api)).render(collection)
